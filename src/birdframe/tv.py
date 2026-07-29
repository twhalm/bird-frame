"""Driving a Samsung Frame's Art Mode.

The browser was the wrong place to put this. A Frame showing a web page is just
a bright TV: the panel runs at TV brightness, the ambient light sensor does
nothing, and the motion sensor cannot turn it off when you leave the room. Art
Mode is the mode the panel was built for, and it takes an uploaded picture.

So BirdFrame composes the wall itself (see compose.py) and pushes it over the
TV's websocket API, using NickWaterton's samsung-tv-ws-api fork:

    https://github.com/NickWaterton/samsung-tv-ws-api

Everything that touches the network happens on the driver thread. The web
request handlers only ever read a status snapshot or set a flag and wake it, so
a TV that has been unplugged cannot hang an HTTP request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .compose import choose, render_jpeg
from .config import Settings
from .gallery import Detection, Gallery, utcnow_iso
from .plates import PlateIndex

log = logging.getLogger("birdframe.tv")

# How long to wait before trying again after something transient failed - an
# unreachable TV, or a plate that would not download. Short enough that the wall
# comes back on its own after the TV is switched on, long enough that an absent
# TV does not fill the log.
RETRY_SECONDS = 60

# Backstop sleep for when there is nothing to watch for: the switch is off, or no
# TV is configured. Nothing depends on this value - the toggle and every new bird
# set the wake event - so it only bounds how long a lost wakeup could cost.
IDLE_SECONDS = 300

# How long after putting the TV into art mode to believe our own reading of it.
# The panel does not report the new mode instantly, and without this a short
# ART_CHECK_SECONDS would see the old value and turn the switch straight back
# off.
TAKEOVER_GRACE_SECONDS = 15


class TVError(RuntimeError):
    """Anything that went wrong talking to the TV."""


class FrameTV:
    """Thin, synchronous wrapper over the art channel.

    One connection is kept open and reused; any failure drops it so the next
    call reconnects rather than retrying down a half-dead websocket.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._art: Any | None = None

    # ------------------------------------------------------------ connection

    def _channel(self) -> Any:
        if self._art is not None:
            return self._art

        # Imported here rather than at module scope so that a missing or broken
        # samsungtvws still lets the app boot and report the problem in /healthz
        # instead of crashing gunicorn on import.
        try:
            from samsungtvws import SamsungTVWS
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise TVError(f"samsungtvws is not installed: {exc}") from exc

        self.settings.tv_token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            tv = SamsungTVWS(
                host=self.settings.tv_host,
                port=self.settings.tv_port,
                # Persisted, or the TV asks you to authorise the connection on
                # screen every single restart.
                token_file=str(self.settings.tv_token_file),
                name=self.settings.tv_name,
            )
            art = tv.art()
            if not art.supported():
                raise TVError(f"{self.settings.tv_host} does not support Art Mode")
        except TVError:
            raise
        except Exception as exc:
            raise TVError(f"{type(exc).__name__}: {exc}") from exc

        log.info("connected to the Frame at %s", self.settings.tv_host)
        self._art = art
        return art

    def close(self) -> None:
        art, self._art = self._art, None
        if art is None:
            return
        try:
            art.close()
        except Exception as exc:
            log.debug("closing the art channel raised: %s", exc)

    def _call(self, what: str, fn: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._channel(), fn)(*args, **kwargs)
        except TVError:
            raise
        except Exception as exc:
            # The socket is suspect now; drop it so the next call reconnects.
            self.close()
            raise TVError(f"{what} failed: {type(exc).__name__}: {exc}") from exc

    # --------------------------------------------------------------- actions

    def upload(self, jpeg: bytes) -> str:
        """Send one composed image and return the id the TV filed it under."""
        content_id = self._call(
            "upload",
            "upload",
            jpeg,
            file_type="jpg",
            # We drew our own mat, so the TV must not draw a second one around
            # it. "none" is Samsung's own id for no matte.
            matte="none",
            portrait_matte="none",
        )
        if not content_id:
            raise TVError("upload returned no content id")
        return str(content_id)

    def select(self, content_id: str) -> None:
        self._call("select", "select_image", content_id, show=True)

    def delete(self, content_ids: list[str]) -> None:
        if content_ids:
            self._call("delete", "delete_list", content_ids)

    def set_art_mode(self, on: bool) -> None:
        self._call("set art mode", "set_artmode", on)

    def art_mode_on(self) -> bool:
        return str(self._call("get art mode", "get_artmode")).lower() == "on"


class ArtDriver:
    """Background thread that keeps the Frame showing the newest bird.

    While enabled it composes the wall, uploads it, selects it and drops the
    stale uploads - then does nothing until what should hang actually changes.
    There is no rotation: the wall is the most recent detection, so the only
    reasons to send anything are a new bird, the toggle, or a failed attempt to
    retry. While disabled it puts the TV back to sleep and waits on the toggle.

    It still wakes on a timer to check whether somebody has taken the panel back
    (see _surrendered), which is a different job from deciding what to hang.
    """

    def __init__(self, settings: Settings, gallery: Gallery, index: PlateIndex) -> None:
        self.settings = settings
        self.gallery = gallery
        self.index = index
        self.tv = FrameTV(settings)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Interruptible sleep: a toggle or a new bird should act now, not after
        # the rest of a fifteen minute nap.
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._enabled = settings.art_on_start
        self._uploads: deque[str] = deque(maxlen=max(1, settings.tv_keep_uploads))
        self._preview: bytes | None = None
        self._showing: list[dict[str, Any]] = []
        # What is on the wall, as two gates. Both are owned by the driver thread.
        #
        # _hung_revision is the cheap one: if the gallery has not changed at all
        # there is no point running choose(), which can go to the network for a
        # plate. It is not authoritative - the same species heard twice moves the
        # revision without changing the picture.
        #
        # _hung_key is authoritative: the plate numbers actually hanging. Every
        # upload is a ~1.5MB write to the TV's flash, so this is what stops the
        # dawn chorus rewriting the same image every couple of minutes.
        #
        # None means nothing has been hung yet; () means the bare mat is up. The
        # two must stay distinct or an empty gallery re-uploads the mat forever.
        self._hung_revision: int | None = None
        self._hung_key: tuple[int | None, ...] | None = None
        # Composed and pushed are different events: with no TV configured
        # nothing is ever pushed, but the wall is still being composed for the
        # preview, and the page needs something to notice that by. A counter
        # rather than a timestamp because two composes can land inside one
        # clock tick, and then the page never reloads the image.
        self._generation = 0
        self._last_push: str | None = None
        self._last_error: str | None = None
        self._art_mode: bool | None = None
        # Set once the TV has been told to leave art mode, so a disabled driver
        # is not sending set_artmode(False) every time it is woken.
        self._settled = False
        # True between a confirmed push and giving the panel back. Only while
        # holding does an "art mode is off" reading mean somebody took the TV;
        # before the first push it just means we have not put anything up yet.
        self._holding = False
        self._held_since = 0.0
        # Why the switch went off on its own, for the page to explain itself.
        self._off_reason: str | None = None

        self._load_state()

    # ----------------------------------------------------------------- state

    def _load_state(self) -> None:
        """Restore the toggle and the list of pictures we left on the TV.

        Without the upload list a restart orphans every picture BirdFrame ever
        sent, and the TV's storage fills up with old birds.
        """
        path = self.settings.tv_state_file
        try:
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not restore TV state: %s", exc)
            return
        if not isinstance(raw, dict):
            return

        if isinstance(raw.get("enabled"), bool):
            self._enabled = raw["enabled"]
        uploads = raw.get("uploads")
        if isinstance(uploads, list):
            self._uploads.extend(str(u) for u in uploads if u)

    def _save_state(self) -> None:
        path = self.settings.tv_state_file
        try:
            with self._lock:
                body = {"enabled": self._enabled, "uploads": list(self._uploads)}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not save TV state: %s", exc)

    # --------------------------------------------------------------- control

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="art", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.tv.close()

    def wake(self) -> None:
        """Nudge the thread. Called by the gallery when a new bird lands."""
        self._wake.set()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            if self._enabled == on:
                return
            self._enabled = on
            self._settled = False
            self._last_error = None
            self._off_reason = None
            self._holding = False
            # Forget what we think is hanging. Going dark takes the TV out of art
            # mode, so whatever we last recorded is no longer on the panel - and
            # without this, turning the switch back on with no new bird in the
            # meantime would find both gates satisfied and do nothing at all.
            # Inside the lock because these are written from a request thread
            # while the driver thread may be part way through hanging.
            self._hung_revision = None
            self._hung_key = None
        log.info("art mode %s", "on" if on else "off")
        self._save_state()
        self.wake()

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        log.info(
            "art driver started: %s", self.settings.tv_host or "no TV configured"
        )
        while not self._stop.is_set():
            delay = self._tick()
            self._wake.wait(delay)
            self._wake.clear()
        log.info("art driver stopped")

    def _tick(self) -> float:
        """One cycle. Returns how long to sleep before the next one.

        Nothing here is on a rotation clock. The wall changes when the gallery
        does; the timer exists only so the driver notices somebody picking up the
        remote, and so a transient failure gets another go.
        """
        if not self.enabled:
            self._go_dark()
            return IDLE_SECONDS

        # Before the change check, deliberately: this can turn the switch off, and
        # a bird landing mid-programme must not drag the panel back to birds.
        if self._surrendered():
            return self._idle()

        if self.gallery.revision == self._hung_revision:
            # Nothing new has been heard, so there is nothing to look at. Just a
            # TV check.
            return self._idle()

        return self._idle() if self._hang() else RETRY_SECONDS

    def _idle(self) -> float:
        """How long to sleep with the wall in the state we want it.

        With a TV that is the art-mode check interval. Without one there is no
        panel to lose, so nothing needs watching and a new bird wakes us anyway.
        """
        if self.settings.tv_configured:
            return float(self.settings.art_check_seconds)
        return float(IDLE_SECONDS)

    def _surrendered(self) -> bool:
        """Has somebody picked up the remote and left Art Mode?

        The Frame's power button switches between the TV and Art Mode, so this
        is what happens every time anyone sits down to watch something. Without
        it the next bird would call set_artmode(True) and drag the panel back to
        birds part way through their programme.

        Returns True when the switch has just been turned off in response.
        """
        if not (self.settings.tv_configured and self._holding):
            return False
        # The TV takes a moment to report the mode we just put it in.
        if time.monotonic() - self._held_since < TAKEOVER_GRACE_SECONDS:
            return False

        try:
            still_ours = self.tv.art_mode_on()
        except TVError as exc:
            # Unreachable is NOT the same as "switched to TV". A Frame that is
            # off at the wall, or a dropped websocket, must not silently flip
            # the switch off - that is an outage to retry, not an instruction.
            log.debug("could not read art mode: %s", exc)
            return False

        if still_ours:
            return False

        log.info("the TV left art mode; turning the switch off")
        with self._lock:
            self._enabled = False
            self._holding = False
            self._art_mode = False
            # Art mode is already off, so there is nothing to tell the TV.
            self._settled = True
            self._off_reason = "switched off at the TV"
        self._save_state()
        return True

    def _hang(self) -> bool:
        """Put the newest bird on the wall.

        Returns True when the wall is showing the current birds as far as we can
        tell - including when it already was and nothing needed sending. False
        means something transient failed and the caller should retry; nothing is
        recorded as hung in that case, or the failure would stick until the next
        bird sang.
        """
        # Revision before the snapshot. The other way round can record a revision
        # that covers a bird which was not in the snapshot we acted on, and that
        # bird would then wait for the next one to arrive before it ever hung.
        revision = self.gallery.revision
        items = self.gallery.snapshot()
        slots = choose(items, self._path_for)

        with self._lock:
            self._showing = [
                {"plate": d.plate, "common_name": d.common_name} for d, _ in slots
            ]

        if not slots and items:
            # There are birds; their plates just are not on disk yet. Retry rather
            # than hanging the bare mat and calling it done - latching here would
            # leave the wall blank until the next bird sang, which is exactly what
            # restoring the history is meant to avoid.
            log.info("no plate available for %d bird(s) yet; will retry", len(items))
            return False

        # Plate numbers, not detections: the same species heard twice resolves to
        # the same plate, and it is the picture that matters.
        key = tuple(d.plate for d, _ in slots)
        if key == self._hung_key:
            # Same birds already up. Sending it again would cost a flash write for
            # an identical image. Record the revision so the next tick does not
            # even look.
            self._hung_revision = revision
            return True

        # Nothing heard at all: the bare mat, so the wall is board rather than
        # whatever happened to be there before.
        if not self._compose([path for _, path in slots]):
            # _preview still holds the previous wall. Pushing it now would put the
            # old image up and record the new one as hanging.
            return False

        if not self.settings.tv_configured:
            # No TV: composing is the whole job, for the preview page.
            self._hung_revision, self._hung_key = revision, key
            return True

        try:
            self._push()
        except TVError as exc:
            log.warning("push to the Frame failed: %s", exc)
            with self._lock:
                self._last_error = str(exc)
            return False

        self._hung_revision, self._hung_key = revision, key
        return True

    def _go_dark(self) -> None:
        """Disabled: take the TV out of art mode once, then wait on the toggle."""
        # Whoever has the panel now, it is not us.
        self._holding = False
        if self._settled or not self.settings.tv_configured:
            self._settled = True
            return
        try:
            self.tv.set_art_mode(False)
            with self._lock:
                self._art_mode = False
                self._last_error = None
            self._settled = True
        except TVError as exc:
            # A TV that is already off cannot be told to turn art mode off, and
            # that is fine - it is off. Do not retry in a tight loop.
            log.info("could not leave art mode: %s", exc)
            with self._lock:
                self._last_error = str(exc)
            self._settled = True
        self.tv.close()

    def _push(self) -> None:
        """Upload the current composition, show it, and tidy up behind it."""
        jpeg = self._preview
        if jpeg is None:
            return

        content_id = self.tv.upload(jpeg)
        self.tv.select(content_id)

        # Art mode last: selecting a picture while the TV is on the TV source
        # only queues it, and turning art mode on afterwards is what actually
        # puts it on the wall.
        if not self.tv.art_mode_on():
            self.tv.set_art_mode(True)

        # The deque drops the oldest id as this one goes in; that dropped id is
        # the one to delete. Deleting after the select means the picture being
        # shown is never the one being removed.
        #
        # Only ids BirdFrame recorded are ever deleted. The TV's My Pictures
        # holds the owner's own uploads too and there is nothing in the content
        # id to tell them apart, so "tidy up anything that looks like ours" would
        # eventually eat somebody's holiday photos. If the state file is lost,
        # those uploads are orphaned and have to be removed from the TV by hand -
        # that is the safe direction to fail in.
        stale = self._uploads[0] if len(self._uploads) == self._uploads.maxlen else None
        self._uploads.append(content_id)
        if stale and stale != content_id:
            try:
                self.tv.delete([stale])
            except TVError as exc:
                log.info("could not delete old upload %s: %s", stale, exc)

        with self._lock:
            self._art_mode = True
            self._last_push = utcnow_iso()
            self._last_error = None
            # We have the panel. From here an "off" reading means somebody took
            # it back, rather than meaning we have not put anything up yet.
            self._holding = True
        self._held_since = time.monotonic()
        self._save_state()

    # ------------------------------------------------------------- composing

    def _path_for(self, detection: Detection) -> Path | None:
        if detection.plate is None or not detection.file_name:
            return None
        return self.index.ensure_cached(detection.plate, detection.file_name)

    def _compose(self, plates: list[Path]) -> bool:
        """Render the wall. False means _preview still holds the previous one."""
        try:
            composed = render_jpeg(
                plates,
                size=self.settings.frame_size,
                light=self.settings.light,
                bevel=self.settings.bevel_px,
                texture=self.settings.mat_texture,
            )
        except Exception as exc:
            log.warning("could not compose the wall: %s", exc)
            with self._lock:
                self._last_error = f"compose failed: {exc}"
            return False
        self._preview = composed
        with self._lock:
            self._generation += 1
        return True

    def preview(self) -> bytes:
        """The current composition, for the web UI. Composed on demand if the
        driver has not produced one yet, so the page is never blank.

        Called from request threads. Reading the reference once means a driver
        compose landing mid-call hands back the old image rather than a torn
        one, and the on-demand bare mat can only ever fill a genuinely empty
        slot - it cannot stomp a composition the driver just made.
        """
        current = self._preview
        if current is None:
            self._compose([])
            current = self._preview
        return current or b""

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": self.settings.tv_configured,
                "host": self.settings.tv_host or None,
                "enabled": self._enabled,
                "art_mode": self._art_mode,
                "showing": list(self._showing),
                "uploads": len(self._uploads),
                "generation": self._generation,
                "off_reason": self._off_reason,
                "last_push": self._last_push,
                "last_error": self._last_error,
            }
