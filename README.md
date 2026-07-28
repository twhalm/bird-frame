# BirdFrame

Puts the matching Audubon plate on a Samsung Frame, in Art Mode, when BirdNET-Go
hears a bird.

![two portrait plates](docs/frame.png)

The web UI is one switch. On hangs the birds, off sends the panel back to sleep.

## Run it

The image is published to GHCR and is public, so nothing needs authenticating.
Only released versions are published — there is no `:latest`, so pick a version
from [releases](https://github.com/twhalm/bird-frame/releases):

```bash
docker run -d --name birdframe -p 8088:8088 \
  -v birdframe-cache:/cache \
  -e PORT=8088 \
  -e BIRDNET_URL=http://birdnet-go:8080 \
  -e TV_HOST=192.168.1.50 \
  ghcr.io/twhalm/bird-frame:1.0.0
```

Or as a compose stack — this is the whole thing, and works as-is in Portainer
(Stacks -> Add stack -> Web editor):

```yaml
services:
  birdframe:
    image: ghcr.io/twhalm/bird-frame:1.0.0
    container_name: birdframe
    restart: unless-stopped
    ports:
      - "8088:8088"
    environment:
      - PORT=8088
      - BIRDNET_URL=http://birdnet-go:8080
      - TV_HOST=192.168.1.50
      - TZ=America/Los_Angeles
    volumes:
      - birdframe-cache:/cache

volumes:
  birdframe-cache:
```

Two settings matter: `BIRDNET_URL` for where the birds come from, and `TV_HOST`
for where they go. If BirdNET-Go runs in a different stack, either use its host
IP or attach this service to its network so the container name resolves.

Then open `http://<docker-host>:8088` and flip the switch.

## Pairing with the TV

Give the TV a static lease on your router first — a Frame that changes address
is a wall that silently stops updating.

The first time BirdFrame connects, the TV shows a prompt asking whether to allow
a device called *BirdFrame*. Accept it. The token that comes back is written to
`/cache/tv-token.txt`, so the prompt does not come back on every restart. If you
ever decline it, delete that file and toggle the switch again.

If nothing happens, the switch subtitle carries the reason — that is the same
string as `last_error` in `/api/tv`. The two common ones:

* *does not support Art Mode* — the TV is not a Frame. The API reports this from
  the TV's own device info; there is nothing to configure.
* *connection refused* / *timed out* — wrong address, or the TV is fully powered
  down rather than in standby. BirdFrame retries once a minute on its own, so
  switching the TV on is enough.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `BIRDNET_URL` | *(empty)* | BirdNET-Go base URL. Empty means webhook-only. |
| `TV_HOST` | *(empty)* | The Frame's IP. Empty means compose but never push — the preview still works, which is handy for tuning the mat. |
| `TV_PORT` | `8002` | The secure websocket. Only change this if you know why. |
| `TV_NAME` | `BirdFrame` | The name shown on the TV's pairing prompt. |
| `ROTATE_SECONDS` | `900` | How long one composition hangs. See below. |
| `ART_ON_START` | `false` | Start driven rather than waiting for the switch. The switch's own position is remembered across restarts regardless; this only sets the very first state. |
| `TV_KEEP_UPLOADS` | `3` | Pictures kept on the TV before the oldest is deleted. |
| `LIGHT` | `-35,40` | Bevel light azimuth and elevation, in degrees. |
| `FRAME_WIDTH` / `FRAME_HEIGHT` | `3840` / `2160` | Compose at panel resolution so the TV never rescales. |
| `POLL_SECONDS` | `60` | How often to check for detections. |
| `POLL_LIMIT` | `15` | Detections requested per poll. |
| `MIN_CONFIDENCE` | `0.65` | Ignore anything less confident. A detection with no confidence field is also ignored. |
| `HISTORY_SIZE` | `40` | How many detections stay in rotation. |
| `WEBHOOK_TOKEN` | *(unset)* | Shared secret for `POST /webhook`, `POST /api/tv` and `POST /api/demo`. Unset leaves them open. |
| `CACHE_DIR` | `/cache` | Where plates, history and the TV token are cached. |
| `CA_BUNDLE` | *(unset)* | PEM for a TLS-inspecting proxy's CA, if you need one. |
| `VERIFY_TLS` | `true` | Set `false` to skip certificate verification for plate downloads. Last resort — prefer `CA_BUNDLE`. |
| `PORT` | `8080` | Port inside the container. BirdNET-Go usually holds 8080, so the examples above use 8088. Publish the same port you set. |
| `LOG_LEVEL` | `INFO` | |
| `DEV` | `false` | Re-read the template on every request. Costs a stat per hit; development only. |
| `BIRDFRAME_VERSION` | `dev` | Set by the release build from the git tag, and reported by `/healthz`. Leave alone. |

If you set `WEBHOOK_TOKEN`, the page needs `?token=...` for the switch to work.
Reading status stays open, since the page cannot render at all without it and
there is nothing sensitive in it.

### Why the rotation is slow by default

Every change is an upload to the TV's internal flash — about 1.5MB, four times an
hour at the default. Fifteen minutes is a picture you notice changing when you
walk past, not a slideshow. Turning it down to seconds works and will write tens
of gigabytes a week to a panel that was not built for it.

A newly heard bird does not wait for the interval. The gallery wakes the driver,
so a first-of-the-year cardinal is on the wall within a few seconds.

## Why it polls instead of using webhooks

BirdNET-Go only fires detection webhooks for new species. `detection_consumer.go`
returns early unless `event.IsNewSpecies()`, so a webhook-only setup shows a
cardinal once and then sits still for months. BirdFrame polls
`/api/v2/detections/recent` instead, which needs no auth and returns everything.

`POST /webhook` still works if you want first-of-year alerts. It reads
`scientific_name`, `species`, `confidence` and `timestamp` from either the top
level or `metadata`, and takes confidence as `0.93` or `93`.

## Species matching

Audubon named these birds in the 1830s, so only about 32% of plate names match a
modern eBird name. Fuzzy matching was tried and produces wrong birds:
"Golden-winged Woodpecker" is a Northern Flicker but fuzzy-matches Golden-naped
Woodpecker, and Wikipedia sends "Le petit caporal" to Napoleon.

So there is no guessing. `src/birdframe/data/curated_map.json` holds 217
hand-verified mappings keyed by scientific name, which is what BirdNET-Go sends.
Unmatched species are skipped rather than approximated. To add one:

```json
"Sitta carolinensis": { "plate": 152, "audubon": "White-breasted Black-capped Nuthatch" }
```

Repeated plate numbers are fine. Audubon often drew several species on one sheet.

`/healthz` lists the species it had no plate for under `unmatched_species` —
those are the ones worth adding.

## Art source

The 435 plates of *The Birds of America*, from
[nathanbuchar/audubon-bird-plates-for-supernote](https://github.com/nathanbuchar/audubon-bird-plates-for-supernote)
— the same plates downsized so the smallest dimension is 2000px, which is enough
for a 4K screen at a quarter of the bytes. Plates are fetched on first sighting of
a species and cached in the `/cache` volume, so the container image stays around
150MB and disk only grows with birds you actually get. All 435 would be about
490MB. The `download` URLs inside the upstream `data.json` are dead, so BirdFrame
fetches from the repo itself.

Public domain. Credit: Courtesy of the John James Audubon Center at Mill Grove,
Montgomery County Audubon Collection, and Zebra Publishing.

## Layout

```
src/birdframe/config.py    settings, read from the environment once
src/birdframe/plates.py    plate resolution, lazy fetch and cache
src/birdframe/payload.py   parsing the shapes BirdNET-Go sends
src/birdframe/gallery.py   rotation, history, stats, disk mirror
src/birdframe/poller.py    the BirdNET-Go poller
src/birdframe/compose.py   the mat, the bevel, and what hangs next to what
src/birdframe/tv.py        the Frame's art channel, and the driver thread
src/birdframe/app.py       create_app() and the routes
src/birdframe/wsgi.py      gunicorn entrypoint
src/birdframe/data/        plates.json, curated_map.json
src/birdframe/web/         index.html, the switch
tests/                     pytest suite
```

Endpoints: `/`, `/api/tv` (GET status, POST `{"enabled": bool}`), `/preview.jpg`,
`/api/current`, `/plate/<n>`, `/webhook`, `/api/demo`, `/healthz`.

Everything that touches the TV happens on the driver thread. The route handlers
only read a status snapshot or set a flag and wake it, so a TV that has been
unplugged cannot hang an HTTP request — `POST /api/tv` returns immediately and
you watch `last_error` for whether it landed.

`/healthz` returns 503 when the poller has gone three cycles without a success, so
the container healthcheck notices a BirdNET-Go outage instead of reporting healthy
while the wall goes stale. In webhook-only mode there is no poller to be unhealthy
about and it always returns 200. The TV is reported there too but deliberately
does not affect the status code: a Frame switched off at the wall is a normal
evening, not a container worth restarting.

History, the switch position and the list of pictures left on the TV are all
mirrored to the cache volume, so a restart brings the wall back and does not
orphan old uploads. Runs as one gunicorn worker on purpose, since the rotation,
the poller and the driver live in process memory.

## Development

Uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # create .venv and install everything
uv run python -m birdframe    # http://localhost:8080, CACHE_DIR=.localcache
uv run pytest                 # tests
uv run ruff format .          # format
uv run ruff check --fix .     # lint
uv run mypy                   # typecheck (strict)
```

`uv sync` needs `git` on PATH: samsungtvws is a pinned git reference rather than
the PyPI package of that name, which is a different maintainer's.

Local run without a container, and without a TV — it composes but does not push,
so `/preview.jpg` is a fast way to iterate on the mat:

```bash
CACHE_DIR=.localcache DEV=true uv run python -m birdframe
```

## License

MIT, see [LICENSE](LICENSE). The plate images are public domain and are not
covered by it.
