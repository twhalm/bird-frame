"""BirdFrame - turns BirdNET-Go detections into Audubon plates on a screen.

Two ways in:

  * POST /webhook       - BirdNET-Go pushes a detection here.
  * poller (optional)   - we poll BirdNET-Go's /api/v2/detections/recent.

The poller exists because BirdNET-Go's webhook only fires for *new* species by
default (see internal/notification/detection_consumer.go: it returns early
unless event.IsNewSpecies()). If you rely on webhooks alone, the art changes
the first time a cardinal shows up and then basically never again. Polling the
recent-detections endpoint gives you every detection, so the wall stays alive.
Enable whichever you like; both feed the same gallery.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort, jsonify, render_template, send_file

from plates import CACHE_DIR, PlateIndex

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("birdframe")

app = Flask(__name__, template_folder="../web", static_folder="../web/static")
# The page is a single template; re-read it from disk on each request so you can
# restyle the frame without restarting the container.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

index = PlateIndex()

# ---------------------------------------------------------------- config

MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.65"))
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "40"))
BIRDNET_URL = os.environ.get("BIRDNET_URL", "").rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
SHOW_UNMATCHED = os.environ.get("SHOW_UNMATCHED", "false").lower() == "true"

# ---------------------------------------------------------------- state

_lock = threading.Lock()
_history: deque[dict] = deque(maxlen=HISTORY_SIZE)
_seen_keys: deque[str] = deque(maxlen=500)
_stats = {"received": 0, "matched": 0, "unmatched": 0, "rejected": 0}

# The gallery lives in memory, so a restart would leave an empty frame on the
# wall until the next bird sings. Mirror it to the cache volume instead.
_STATE_FILE = CACHE_DIR.parent / "history.json"


def _save_history() -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            items = list(_history)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as exc:
        log.debug("could not save history: %s", exc)


def _load_history() -> None:
    try:
        if not _STATE_FILE.exists():
            return
        items = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            return
        with _lock:
            for item in reversed(items[:HISTORY_SIZE]):
                if isinstance(item, dict) and item.get("plate"):
                    _history.appendleft(item)
        log.info("restored %d detections from previous run", len(_history))
    except Exception as exc:
        log.warning("could not restore history: %s", exc)


def _record(scientific: str, common: str, confidence: float, when: str, source: str) -> bool:
    """Add a detection to the gallery. Returns True if it was displayed."""
    with _lock:
        _stats["received"] += 1

    if confidence and confidence < MIN_CONFIDENCE:
        with _lock:
            _stats["rejected"] += 1
        log.debug("below threshold: %s (%.2f)", common or scientific, confidence)
        return False

    plate = index.resolve(scientific, common)
    if not plate:
        with _lock:
            _stats["unmatched"] += 1
        log.info("no plate for %s / %s", scientific, common)
        if not SHOW_UNMATCHED:
            return False

    entry = {
        "scientific_name": scientific,
        "common_name": common or (scientific or "Unknown"),
        "confidence": round(confidence, 4) if confidence else None,
        "detected_at": when,
        "source": source,
    }
    if plate:
        entry.update(plate)
        # Warm the cache off the request thread so the webhook returns fast.
        threading.Thread(
            target=index.ensure_cached,
            args=(plate["plate"], plate["file_name"]),
            daemon=True,
        ).start()
        with _lock:
            _stats["matched"] += 1

    with _lock:
        _history.appendleft(entry)
    _save_history()

    log.info(
        "%s -> %s",
        entry["common_name"],
        f"plate {plate['plate']} ({plate['audubon_name']})" if plate else "unmatched",
    )
    return True


def _dedupe_key(scientific: str, common: str, when: str) -> str:
    return f"{scientific or common}@{when}"


# ---------------------------------------------------------------- webhook


@app.post("/webhook")
def webhook():
    """Accept a BirdNET-Go notification.

    BirdNET-Go's default detection payload puts the species in .Title and the
    details in .Metadata, but the template is user-configurable, so we accept
    several shapes rather than insisting on one.
    """
    payload = None
    try:
        payload = __import__("flask").request.get_json(force=True, silent=True)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        log.warning("webhook: unparseable body")
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    meta = payload.get("metadata") or payload.get("Metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    def pick(*keys):
        for k in keys:
            for src in (payload, meta):
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return None

    scientific = pick("scientific_name", "scientificName", "sciName")
    common = pick("species", "common_name", "commonName", "title", "Title", "bird")

    raw_conf = pick("confidence", "Confidence")
    try:
        confidence = float(raw_conf) if raw_conf is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    # BirdNET-Go may send either a 0-1 ratio or a percentage.
    if confidence > 1.0:
        confidence /= 100.0

    when = (
        pick("timestamp", "Timestamp", "time", "date")
        or datetime.now(timezone.utc).isoformat()
    )

    if not scientific and not common:
        log.warning("webhook: no species field found in %s", list(payload)[:8])
        return jsonify({"ok": False, "error": "no species field in payload"}), 400

    shown = _record(str(scientific or ""), str(common or ""), confidence, str(when), "webhook")
    return jsonify({"ok": True, "displayed": shown})


# ---------------------------------------------------------------- poller


def _poll_loop() -> None:
    """Poll BirdNET-Go for recent detections.

    /api/v2/detections/recent is unauthenticated and returns scientificName,
    commonName, confidence and timestamp for every detection - not just new
    species - which is what makes a continuously changing gallery possible.
    """
    url = f"{BIRDNET_URL}/api/v2/detections/recent"
    log.info("poller started: %s every %ds", url, POLL_SECONDS)
    while True:
        try:
            r = requests.get(url, params={"limit": 15}, timeout=15)
            r.raise_for_status()
            data = r.json()
            rows = data if isinstance(data, list) else data.get("data") or data.get("detections") or []
            # Oldest first so the newest ends up at the front of the gallery.
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                sci = row.get("scientificName") or ""
                com = row.get("commonName") or ""
                when = row.get("timestamp") or f"{row.get('date','')} {row.get('time','')}".strip()
                key = _dedupe_key(sci, com, when)
                with _lock:
                    if key in _seen_keys:
                        continue
                    _seen_keys.append(key)
                try:
                    conf = float(row.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                if conf > 1.0:
                    conf /= 100.0
                _record(sci, com, conf, when or datetime.now(timezone.utc).isoformat(), "poll")
        except Exception as exc:
            log.warning("poll failed: %s", exc)
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- routes


@app.get("/")
def home():
    return render_template("frame.html")


@app.get("/api/current")
def api_current():
    """What the wall should show right now, plus recent history for the ticker."""
    with _lock:
        items = list(_history)
        stats = dict(_stats)
    displayable = [i for i in items if i.get("plate")]
    return jsonify(
        {
            "current": displayable[0] if displayable else None,
            "recent": displayable[:12],
            "stats": stats,
            "count": len(displayable),
        }
    )


@app.get("/plate/<int:plate>")
def plate_image(plate: int):
    info = index.plates.get(plate)
    if not info:
        abort(404)
    path = index.ensure_cached(plate, info["fileName"])
    if not path:
        abort(503, "plate image unavailable")
    # Immutable: a given plate's pixels never change.
    resp = send_file(path, mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/healthz")
def healthz():
    with _lock:
        stats = dict(_stats)
        n = len(_history)
    cached = len(list(CACHE_DIR.rglob("*.jpg"))) if CACHE_DIR.exists() else 0
    return jsonify({"ok": True, "history": n, "cached_plates": cached, "stats": stats})


@app.post("/api/demo")
def api_demo():
    """Seed a few detections so you can see the wall without waiting for a bird."""
    demo = [
        ("Cardinalis cardinalis", "Northern Cardinal", 0.94),
        ("Cyanocitta cristata", "Blue Jay", 0.91),
        ("Colaptes auratus", "Northern Flicker", 0.88),
        ("Zenaida macroura", "Mourning Dove", 0.86),
        ("Baeolophus bicolor", "Tufted Titmouse", 0.83),
        ("Sitta carolinensis", "White-breasted Nuthatch", 0.81),
    ]
    now = datetime.now(timezone.utc).isoformat()
    added = sum(_record(s, c, conf, now, "demo") for s, c, conf in demo)
    return jsonify({"ok": True, "added": added})


if __name__ == "__main__":
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _load_history()
    if BIRDNET_URL:
        threading.Thread(target=_poll_loop, daemon=True).start()
    else:
        log.info("BIRDNET_URL not set - webhook-only mode")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
