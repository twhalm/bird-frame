# BirdFrame

Puts the matching Audubon plate on a Samsung Frame, in Art Mode, when BirdNET-Go
hears a bird.

![two portrait plates](docs/frame.png)

The wall is whatever was heard most recently — two plates when the newest is a
portrait, since a portrait wants a second one beside it. It changes when a bird
is heard, not on a timer.

The web UI is one switch. On hangs the birds, off sends the panel back to sleep.

## Getting it on the TV

**1. Find the Frame's IP.** On the TV: *Settings → General → Network → Network
Status → IP Settings*. Give it a static lease on your router while you are there,
or the wall quietly stops updating the next time the address changes.

**2. Start the container.** This is the whole stack, and works as-is in Portainer
(*Stacks → Add stack → Web editor*):

```yaml
services:
  birdframe:
    image: ghcr.io/twhalm/bird-frame:latest
    container_name: birdframe
    restart: unless-stopped
    ports:
      - "8088:8088"
    environment:
      - PORT=8088
      - BIRDNET_URL=http://birdnet-go:8080   # where the birds come from
      - TV_HOST=192.168.1.50                 # where they go
      - TZ=America/Los_Angeles
    volumes:
      - birdframe-cache:/cache

volumes:
  birdframe-cache:
```

Those two addresses are the only settings that matter. If BirdNET-Go runs in a
different stack, use its host IP or attach this service to its network.

**3. Flip the switch.** Open `http://<docker-host>:8088` and turn on Art Mode.

**4. Accept the pairing prompt.** The TV asks whether to allow a device called
*BirdFrame*. Say yes — this only happens once. The token is kept in
`/cache/tv-token.txt`; delete it and toggle again if you ever need to redo it.

The page shows a preview of exactly what was sent to the TV, so you can tell it
is working without leaving your chair.

### If nothing appears

The switch subtitle carries the reason, same as `last_error` in `GET /api/tv`.

| It says | Do |
|---|---|
| *does not support Art Mode* | The TV is not a Frame. Nothing to configure. |
| *connection refused* / *timed out* | Wrong IP, or the TV is off at the wall rather than in standby. It retries every minute — switching the TV on is enough. |
| *unauthorised* | You set `WEBHOOK_TOKEN`. Open the page as `?token=…`. |
| *switched off at the TV* | Somebody left Art Mode with the remote, so BirdFrame got out of the way. Flip the switch back on. |
| nothing, and no birds | Nothing has been heard above `MIN_CONFIDENCE` yet, or no plate matched. `GET /healthz` lists misses under `unmatched_species`. |

## Pinning a version

`:latest` follows the newest release and only moves after that image has booted
in CI. Pin a version from [releases](https://github.com/twhalm/bird-frame/releases)
instead to decide your own timing; every release keeps its own immutable tag.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `BIRDNET_URL` | *(empty)* | BirdNET-Go base URL. Empty means webhook-only. |
| `TV_HOST` | *(empty)* | The Frame's IP. Empty composes but never pushes, so the preview still works. |
| `TV_PORT` | `8002` | The secure websocket. |
| `TV_NAME` | `BirdFrame` | Name shown on the TV's pairing prompt. |
| `ART_CHECK_SECONDS` | `60` | How often to check the TV is still in Art Mode. If it is not, the switch turns itself off. |
| `ART_ON_START` | `false` | Sets only the very first state; the switch position is remembered across restarts. |
| `TV_KEEP_UPLOADS` | `3` | Pictures kept on the TV before the oldest is deleted. |
| `BEVEL_PX` | `5` | Depth of the 45° cut. 4-ply rag board is ~6px at 55", 5px at 65". |
| `MAT_TEXTURE` | `0` | Mottling on the board. 0 is a flat fill. Try 1.0–1.6. |
| `LIGHT` | `-35,40` | Bevel light azimuth and elevation, in degrees. |
| `FRAME_WIDTH` / `FRAME_HEIGHT` | `3840` / `2160` | Compose at panel resolution. |
| `POLL_SECONDS` | `60` | How often to check for detections. |
| `POLL_LIMIT` | `15` | Detections requested per poll. |
| `MIN_CONFIDENCE` | `0.65` | Ignore anything less confident, including detections with no confidence field. |
| `HISTORY_SIZE` | `40` | How many detections are remembered. Only the newest hangs; the rest fill `/api/current` and supply a pairing partner. |
| `WEBHOOK_TOKEN` | *(unset)* | Shared secret for `POST /webhook` and `/api/tv`. Unset leaves them open. |
| `CACHE_DIR` | `/cache` | Plates, history and the TV token. |
| `CA_BUNDLE` | *(unset)* | PEM for a TLS-inspecting proxy's CA. |
| `VERIFY_TLS` | `true` | `false` skips certificate checks on plate downloads. Prefer `CA_BUNDLE`. |
| `PORT` | `8080` | Port inside the container. BirdNET-Go usually holds 8080. |
| `LOG_LEVEL` | `INFO` | |
| `DEV` | `false` | Re-read the template every request. Development only. |

## Endpoints

`/` · `GET /api/tv` status · `POST /api/tv` `{"enabled": bool}` · `/preview.jpg` ·
`/api/current` · `/plate/<n>` · `POST /webhook` · `/healthz`

`/healthz` returns 503 once the poller has missed three cycles. The TV is
reported there but never affects the status code.

## Adding a species

Only about a third of Audubon's 1830s plate names match a modern eBird name, so
`src/birdframe/data/curated_map.json` maps them by scientific name. Unmatched
species are skipped rather than guessed at. To add one:

```json
"Sitta carolinensis": { "plate": 152, "audubon": "White-breasted Black-capped Nuthatch" }
```

Repeated plate numbers are fine — Audubon often drew several species on one
sheet. `GET /healthz` lists what it had no plate for.

## Development

Uses [uv](https://docs.astral.sh/uv/). Needs `git` on PATH, since samsungtvws is
a pinned git reference rather than the PyPI package of that name.

```bash
uv sync
uv run pytest
uv run ruff format . && uv run ruff check --fix . && uv run mypy

# no container, no TV: composes but does not push, so /preview.jpg
# is a fast way to iterate on the mat
CACHE_DIR=.localcache DEV=true uv run python -m birdframe
```

## Credits

Plates from [nathanbuchar/audubon-bird-plates-for-supernote](https://github.com/nathanbuchar/audubon-bird-plates-for-supernote),
fetched on first sighting and cached in `/cache`. Public domain — courtesy of the
John James Audubon Center at Mill Grove, Montgomery County Audubon Collection,
and Zebra Publishing.

Art Mode is driven through [NickWaterton/samsung-tv-ws-api](https://github.com/NickWaterton/samsung-tv-ws-api).

MIT, see [LICENSE](LICENSE). The plate images are not covered by it.
