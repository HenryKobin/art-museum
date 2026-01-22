#!/usr/bin/env python3
import json
import logging
import random
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, send_from_directory, abort
import yaml
import requests

ESP32_ORB_URL = "http://192.168.1.217/state"


# ----------------- PATHS & CONFIG -----------------

BASE_DIR = Path(__file__).resolve().parent
HOME = Path.home()

ARTISTS_CONFIG_PATH = BASE_DIR / "artists.yaml"

DATA_DIR = BASE_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

METADATA_PATH = DATA_DIR / "pieces.json"

# OnnxStream SD config (SDXL Turbo) – adjust if you changed paths
SD_BIN = HOME / "OnnxStream" / "src" / "build" / "sd"
SD_MODELS_DIR = HOME / "onnx_models"  # parent folder you used with --download

# 30 minutes between new pieces
GENERATION_INTERVAL_SECONDS = 30 * 60

# llama.cpp server endpoint
LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"

# ESP32 orb endpoint (your LCD eye)
ORB_BASE_URL = "http://192.168.1.216"
ORB_STATE_URL = f"{ORB_BASE_URL}/state"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)

# ----------------- ARTIST CONFIG -----------------


def load_artists_config():
    with ARTISTS_CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    artists_list = cfg.get("artists", [])
    artists_by_id = {a["id"]: a for a in artists_list}
    selection = cfg.get("selection", {}) or {}
    return cfg, artists_by_id, selection


ARTISTS_CONFIG, ARTISTS_BY_ID, ARTIST_SELECTION = load_artists_config()


def choose_artist(requested_id=None):
    """
    Pick which artist to use for this generation.

    - If requested_id is given and valid, use it.
    - Else use selection.mode: 'manual' or 'weighted_random'.
    """
    if not ARTISTS_BY_ID:
        raise RuntimeError("No artists defined in artists.yaml")

    # explicit request wins
    if requested_id and requested_id in ARTISTS_BY_ID:
        return ARTISTS_BY_ID[requested_id]

    mode = (ARTIST_SELECTION.get("mode") or "weighted_random").lower()
    default_id = ARTIST_SELECTION.get("default") or next(iter(ARTISTS_BY_ID.keys()))

    if mode == "manual":
        # always use the default artist
        return ARTISTS_BY_ID.get(default_id, next(iter(ARTISTS_BY_ID.values())))

    # weighted_random (default)
    artists = list(ARTISTS_BY_ID.values())
    weights = [a.get("weight", 1) for a in artists]
    return random.choices(artists, weights=weights, k=1)[0]


# ----------------- ORB / ESP32 STATE -----------------

def send_orb_state(artist_id: str, state: str = "FINISHED") -> None:
    """
    Notify the ESP32 orb which artist + state should be shown.
    artist_id: e.g. 'pierre', 'inkwell', 'deco9', 'bathys', 'mycelia'
    state: one of: FINISHED, THINKING, DRAWING, DONE
    """
    try:
        payload = {"artist_id": artist_id, "state": state}
        # ESP32 handler already expects JSON in the body
        r = requests.post(ESP32_ORB_URL, json=payload, timeout=1.0)
        if r.status_code != 200:
            logging.warning("Orb returned %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("Failed to update orb state: %s", e)



def update_orb_state(artist_id: str, state: str) -> None:
    """
    Notify the ESP32 orb about current artist + state.

    artist_id: "pierre", "inkwell", etc.
    state: one of "FINISHED", "THINKING", "DRAWING", "DONE"
    """
    payload = {"artist_id": artist_id, "state": state}
    try:
        # small timeout so a dead orb never blocks generation
        resp = requests.post(ORB_STATE_URL, json=payload, timeout=2)
        if resp.status_code != 200:
            logging.warning(
                "Orb state update non-200 (%s): %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logging.warning("Failed to update orb state (%s, %s): %s", artist_id, state, e)


# ----------------- LLM HELPERS -----------------


def _llama_chat(system, user, max_tokens=200):
    """
    Call llama-server over HTTP and return the assistant's text.
    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    """
    payload = {
        "model": "default",  # llama-server's default model
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.8,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
    }

    resp = requests.post(LLAMA_SERVER_URL, json=payload, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return content.strip()


def run_llm(artist):
    """
    High-level orchestrator for a single artist:
      1) Call LLM with artist.scene_system_prompt to get TITLE + SCENE.
      2) Call LLM with artist.commentary_system_prompt to get commentary.
      3) Build final dict: {artist_id, title, image_prompt, commentary}.
    """
    scene_system = artist["scene_system_prompt"]
    commentary_system = artist["commentary_system_prompt"]
    style_prefix = artist["sd_style_prefix"]
    artist_id = artist["id"]

    # --------- Call 1: TITLE + SCENE (no style words) ---------
    scene_user = "Invent one new artwork now. Follow the format exactly."

    raw_scene = _llama_chat(scene_system, scene_user, max_tokens=200)

    title = "UNTITLED"
    scene = ""

    for line in raw_scene.splitlines():
        u = line.strip()
        if u.upper().startswith("TITLE:"):
            title = u.split(":", 1)[1].strip() or "UNTITLED"
        elif u.upper().startswith("SCENE:"):
            scene = u.split(":", 1)[1].strip()

    if not scene:
        logging.warning("Could not parse SCENE from LLM output; using raw text.")
        scene = raw_scene.strip()

    # Compose the full SD prompt by prepending the artist's fixed style
    image_prompt = f"{style_prefix.strip()} {scene}"

    # --------- Call 2: commentary based on TITLE + SCENE ---------
    commentary_user = (
        f"TITLE: {title}\n"
        f"SCENE: {scene}\n\n"
        "Write the gallery wall text / artist note for this piece. "
        "Write in first person as the artist. Remember: "
        "exactly TWO paragraphs, each 4–7 sentences, separated by a blank line. "
        "Do not include labels or restate these instructions."
    )

    commentary_text = _llama_chat(commentary_system, commentary_user, max_tokens=260)
    commentary = commentary_text.strip()

    return {
        "artist_id": artist_id,
        "title": title,
        "image_prompt": image_prompt,
        "commentary": commentary,
    }


# ----------------- METADATA HELPERS -----------------


def load_pieces():
    if not METADATA_PATH.exists():
        return []
    try:
        with METADATA_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # sort by created_at just in case
        return sorted(
            data,
            key=lambda p: p.get("created_at", ""),
        )
    except Exception as e:
        logging.error("Failed to load pieces.json: %s", e)
        return []


def save_pieces(pieces):
    tmp = METADATA_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(pieces, f, ensure_ascii=False, indent=2)
    tmp.replace(METADATA_PATH)


# ----------------- DIFFUSION -----------------


def run_sd(image_prompt, out_path):
    """Run OnnxStream SDXL Turbo to generate an image."""
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(SD_BIN),
        "--turbo",
        "--models-path",
        str(SD_MODELS_DIR),
        "--prompt",
        image_prompt,
        "--steps",
        "3",  # small but not 1, still fast-ish on Pi
        "--rpi",
        "A",  # auto configure for Raspberry Pi
        "--res",
        "512x512",
        "--output",
        str(out_path),
    ]

    logging.info("Calling SDXL Turbo via OnnxStream…")
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        logging.error("sd stderr: %s", proc.stderr[:500])
        raise RuntimeError(f"sd failed: {proc.stderr[:500]}")

    if not out_path.exists():
        raise RuntimeError(f"sd claimed success but {out_path} does not exist")

    return out_path


# ----------------- GENERATION PIPELINE -----------------


def generate_piece(artist):
    """
    End-to-end for a given artist:
      LLM -> SDXL -> save metadata -> return piece dict.

    Tracks artist_id and stores images under data/images/<folder_prefix>/...
    Also drives the ESP32 orb's state machine.
    """
    artist_id = artist["id"]
    folder_prefix = artist.get("folder_prefix", artist_id)
    artist_img_dir = IMAGES_DIR / folder_prefix
    artist_img_dir.mkdir(parents=True, exist_ok=True)

    # --- STATE: THINKING (LLM) ---
    update_orb_state(artist_id, "THINKING")
    meta = run_llm(artist)

    ts = int(time.time())
    filename = f"{ts}.png"
    image_path = artist_img_dir / filename

    # --- STATE: DRAWING (SD) ---
    update_orb_state(artist_id, "DRAWING")
    run_sd(meta["image_prompt"], image_path)

    piece = {
        "id": ts,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "artist_id": artist_id,
        "title": meta["title"].strip(),
        "image_prompt": meta["image_prompt"].strip(),
        "commentary": meta["commentary"].strip(),
        # store path relative to IMAGES_DIR so /images/<path> works
        "image_filename": f"{folder_prefix}/{filename}",
    }

    pieces = load_pieces()
    pieces.append(piece    )
    save_pieces(pieces)

    logging.info("New piece generated: [%s] %s", artist_id, piece["title"])

    # --- STATE: DONE (short celebration, orb code falls back to FINISHED) ---
    update_orb_state(artist_id, "DONE")

    return piece


# ----------------- BACKGROUND WORKER -----------------


def worker_loop():
    # On startup, small delay so everything else can boot
    time.sleep(5)
    while True:
        artist = choose_artist()
        try:
            logging.info("Starting generation for artist %s", artist["id"])
            generate_piece(artist)
        except Exception as e:
            logging.error("Error in generation loop: %s", e)
            #  make sure orb doesn't stay stuck.
            try:
                update_orb_state(artist["id"], "FINISHED")
            except Exception:
                pass
        time.sleep(GENERATION_INTERVAL_SECONDS)


# ----------------- FLASK APP -----------------

app = Flask(__name__, template_folder="templates")


@app.route("/")
def index():
    pieces = load_pieces()
    if not pieces:
        return "Pierre is booting up. No pieces yet.", 200

    total = len(pieces)

    try:
        idx = int(request.args.get("index", total - 1))
    except ValueError:
        idx = total - 1

    idx = max(0, min(total - 1, idx))
    piece = pieces[idx]

    artist_id = piece.get("artist_id")
    if artist_id and artist_id in ARTISTS_BY_ID:
        artist = ARTISTS_BY_ID[artist_id]
    else:
        # fallback if older pieces have no artist_id
        artist = next(iter(ARTISTS_BY_ID.values()))

    # Tell the orb which artist is currently on display.
    # Use FINISHED because we're just viewing, not generating.
    try:
        send_orb_state(artist["id"], "FINISHED")
    except Exception:
        # Don't break the page if the orb is offline
        logging.debug("Could not update orb for viewing state", exc_info=True)

    return render_template(
        "gallery.html",
        current_piece=piece,
        current_artist=artist,
        index=idx,
        total=total,
        has_prev=(idx > 0),
        has_next=(idx < total - 1),
    )


@app.route("/images/<path:filename>")
def images(filename):
    # basic safety 
    path_obj = Path(filename)
    if ".." in path_obj.parts:
        abort(400)
    return send_from_directory(IMAGES_DIR, filename)


def main():
    # Start background generation thread
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    # Run Flask
    app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
