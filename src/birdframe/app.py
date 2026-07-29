"""BirdFrame - turns BirdNET-Go detections into Audubon plates on a Samsung Frame.

Two ways in:

  * POST /webhook  - BirdNET-Go pushes a detection here.
  * the poller     - we poll BirdNET-Go's /api/v2/detections/recent (see poller.py
                     for why this is the one you actually want).

Both feed the same gallery, and the art driver (tv.py) hangs the newest bird in
it on the TV. The web UI is a light switch: on puts the birds in Art Mode, off
sends the panel back to sleep.
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from . import __version__
from .config import WEB_DIR, Settings
from .gallery import Gallery, utcnow_iso
from .payload import parse_webhook
from .plates import PlateIndex
from .poller import Poller
from .tv import ArtDriver

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
    start_driver: bool = True,
    load_history: bool = True,
) -> Flask:
    """Build the application.

    The poller, the art driver and the history restore are wired up here rather
    than at import time, so importing this module has no side effects and tests
    can opt out.
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

    driver = ArtDriver(settings, gallery, index)
    # A bird that lands while the driver is asleep should go up now rather than
    # waiting out the art-mode check interval.
    gallery.on_change = driver.wake
    if start_driver:
        driver.start()
        atexit.register(driver.stop)

    app.extensions["birdframe"] = {
        "settings": settings,
        "index": index,
        "gallery": gallery,
        "poller": poller,
        "driver": driver,
    }

    _register_routes(app, settings, index, gallery, driver)
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
    app: Flask,
    settings: Settings,
    index: PlateIndex,
    gallery: Gallery,
    driver: ArtDriver,
) -> None:
    @app.get("/")
    def home() -> str:
        return render_template("index.html")

    @app.get("/api/tv")
    def api_tv() -> Response:
        """What the Frame is doing. Polled by the toggle page."""
        return jsonify(driver.status())

    @app.post("/api/tv")
    def api_tv_set() -> tuple[Response, int] | Response:
        """Flip Art Mode on or off.

        Returns immediately: the driver thread does the talking, so an
        unreachable TV cannot hang this request. Watch `last_error` in the
        status for whether it actually landed.
        """
        if not _authorised(settings):
            return jsonify({"ok": False, "error": "unauthorised"}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return jsonify({"ok": False, "error": 'expected {"enabled": bool}'}), 400

        driver.set_enabled(payload["enabled"])
        return jsonify({"ok": True, **driver.status()})

    @app.get("/preview.jpg")
    def preview() -> Response:
        """The composition currently on the wall, exactly as the TV has it.

        This is the same renderer the upload uses, so what you see here is what
        is hanging - it is not a second implementation of the mat.
        """
        body = driver.preview()
        if not body:
            abort(503, "nothing composed yet")
        resp = Response(body, mimetype="image/jpeg")
        # It changes every rotation, and it is the only way to tell the page
        # updated at all.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/current")
    def api_current() -> Response:
        """What the wall should show right now, plus the history behind it."""
        # `current` is what hangs; `recent` is everything still remembered. It
        # used to be truncated to 12 here, which quietly capped HISTORY_SIZE.
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
        # redeploy actually took. The TV block is reported but deliberately does
        # NOT affect `ok`: a Frame that is switched off at the wall is a normal
        # evening, not an unhealthy container.
        return jsonify(
            {"ok": ok, "version": __version__, "tv": driver.status(), **body}
        ), (200 if ok else 503)
