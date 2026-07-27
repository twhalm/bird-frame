"""BirdFrame - turns BirdNET-Go detections into Audubon plates on a screen.

Two ways in:

  * POST /webhook  - BirdNET-Go pushes a detection here.
  * the poller     - we poll BirdNET-Go's /api/v2/detections/recent (see poller.py
                     for why this is the one you actually want).

Both feed the same gallery.
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from . import __version__
from .config import WEB_DIR, Settings
from .gallery import DEMO_BIRDS, Gallery, utcnow_iso
from .payload import parse_webhook
from .plates import PlateIndex
from .poller import Poller

log = logging.getLogger("birdframe")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def create_app(
    settings: Settings | None = None,
    *,
    start_poller: bool = True,
    load_history: bool = True,
) -> Flask:
    """Build the application.

    The poller and the history restore are wired up here rather than at import
    time, so importing this module has no side effects and tests can opt out.
    """
    settings = settings or Settings.from_env()

    app = Flask(
        __name__,
        template_folder=str(WEB_DIR),
        static_folder=str(WEB_DIR / "static"),
    )
    # Re-reading the template from disk on every request costs a stat per hit, so
    # it is a development convenience only.
    app.config["TEMPLATES_AUTO_RELOAD"] = settings.dev
    app.jinja_env.auto_reload = settings.dev

    index = PlateIndex(settings)
    gallery = Gallery(settings, index)

    settings.plate_cache_dir.mkdir(parents=True, exist_ok=True)
    if load_history:
        gallery.load()

    poller = Poller(settings, gallery)
    if start_poller:
        poller.start()
        atexit.register(poller.stop)

    app.extensions["birdframe"] = {
        "settings": settings,
        "index": index,
        "gallery": gallery,
        "poller": poller,
    }

    _register_routes(app, settings, index, gallery)
    return app


def _authorised(settings: Settings) -> bool:
    """Check the webhook shared secret, if one is configured.

    Optional: on a trusted LAN there is nothing to protect. When WEBHOOK_TOKEN is
    set, anything that can reach the port must present it, otherwise anyone on the
    network can inject detections.
    """
    if not settings.webhook_token:
        return True

    supplied = request.headers.get("X-Webhook-Token", "")
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:]
    if not supplied:
        supplied = request.args.get("token", "")

    return secrets.compare_digest(supplied, settings.webhook_token)


def _register_routes(
    app: Flask, settings: Settings, index: PlateIndex, gallery: Gallery
) -> None:
    @app.get("/")
    def home() -> str:
        return render_template("frame.html")

    @app.get("/api/current")
    def api_current() -> Response:
        """What the wall should show right now, plus the rotation behind it."""
        # The whole history is the rotation. It used to be truncated to 12 here,
        # which quietly capped HISTORY_SIZE for the page.
        items = gallery.snapshot()
        return jsonify(
            {
                "current": items[0].as_dict() if items else None,
                "recent": [d.as_dict() for d in items],
                "stats": gallery.stats.as_dict(),
                "count": len(items),
            }
        )

    @app.get("/plate/<int:plate>")
    def plate_image(plate: int) -> Response:
        info = index.plates.get(plate)
        if not info:
            abort(404)
        path = index.ensure_cached(plate, info["fileName"])
        if not path:
            abort(503, "plate image unavailable")
        resp = send_file(path, mimetype="image/jpeg", conditional=True)
        # A given plate's pixels never change.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.post("/webhook")
    def webhook() -> tuple[Response, int] | Response:
        """Accept a BirdNET-Go notification."""
        if not _authorised(settings):
            log.warning("webhook: rejected an unauthorised request")
            return jsonify({"ok": False, "error": "unauthorised"}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            log.warning("webhook: body was not a JSON object")
            return jsonify({"ok": False, "error": "expected a JSON object"}), 400

        parsed = parse_webhook(payload)
        if not parsed.has_species:
            log.warning("webhook: no species field found in %s", sorted(payload)[:8])
            return jsonify({"ok": False, "error": "no species field in payload"}), 400

        result = gallery.record(
            parsed.scientific,
            parsed.common,
            parsed.confidence,
            parsed.when or utcnow_iso(),
            "webhook",
        )
        return jsonify(
            {"ok": True, "displayed": result.displayed, "reason": result.reason}
        )

    @app.post("/api/demo")
    def api_demo() -> tuple[Response, int] | Response:
        """Seed a few detections so you can see the wall without waiting for a bird."""
        if not _authorised(settings):
            return jsonify({"ok": False, "error": "unauthorised"}), 401
        now = utcnow_iso()
        added = sum(
            gallery.record(b.scientific, b.common, b.confidence, now, "demo").displayed
            for b in DEMO_BIRDS
        )
        return jsonify({"ok": True, "added": added})

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        """Liveness *and* poller health.

        Returns 503 when the poller has gone quiet for three cycles, so the
        container healthcheck notices a BirdNET-Go outage instead of reporting ok
        while the wall slowly goes stale.
        """
        body = gallery.health()
        ok = gallery.is_healthy()
        # Which release is on the wall, so you can tell from Portainer whether a
        # redeploy actually took.
        return jsonify({"ok": ok, "version": __version__, **body}), (200 if ok else 503)
