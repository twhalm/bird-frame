# BirdFrame

Puts the matching Audubon plate on a TV when BirdNET-Go hears a bird. Fullscreen,
no text, no UI. Two portrait plates hang as a pair; a landscape plate fills the
mat alone.

![two portrait plates](docs/frame.png)

## Run it

```bash
docker compose up -d --build
```

Open `http://<docker-host>:8080` and press F11. Press `d` on the page to seed
demo birds without waiting for a real detection.

Set `BIRDNET_URL` in `docker-compose.yml` to your BirdNET-Go address. That is the
only setting that matters.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `BIRDNET_URL` | *(empty)* | BirdNET-Go base URL. Empty means webhook-only. |
| `POLL_SECONDS` | `60` | How often to check for detections. |
| `MIN_CONFIDENCE` | `0.65` | Ignore anything less confident. |
| `HISTORY_SIZE` | `40` | How many detections stay in rotation. |
| `SHOW_UNMATCHED` | `false` | Show species with no verified plate. |
| `CA_BUNDLE` | *(unset)* | PEM for a TLS-inspecting proxy's CA, if you need one. |

URL options: `?rotate=150&poll=20` (seconds) and `?light=-35,40` (bevel light
azimuth and elevation, in degrees).

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

So there is no guessing. `data/curated_map.json` holds 217 hand-verified mappings
keyed by scientific name, which is what BirdNET-Go sends. Unmatched species are
skipped rather than approximated. To add one:

```json
"Sitta carolinensis": { "plate": 152, "audubon": "White-breasted Black-capped Nuthatch" }
```

Repeated plate numbers are fine. Audubon often drew several species on one sheet.

Rising `unmatched` in `/healthz` means species worth adding. The logs name them.

## The mat

Rag board is one colour throughout, so the bevel is board colour under different
light, not a white core. Each of the four faces is flat, so each gets one flat
tone from Lambertian shading of its normal. The cut flares toward the viewer, so
light from above leaves the top face darkest and the bottom face brightest. The
corners are real 45 degree mitres. The board is flush with the print, so nothing
casts a shadow onto the art.

![the bevel at 9x](docs/bevel-detail.png)

## Art source

The 435 plates of *The Birds of America*, from
[nathanbuchar/audubon-bird-plates](https://github.com/nathanbuchar/audubon-bird-plates)
at 2000px. Plates are fetched on first sighting of a species and cached in the
`/cache` volume, so the image stays near 190MB. The `download` URLs inside that
repo's `data.json` are dead, so BirdFrame fetches from the repo itself.

Public domain. Credit: Courtesy of the John James Audubon Center at Mill Grove,
Montgomery County Audubon Collection, and Zebra Publishing.

## Layout

```
app/plates.py   plate resolution, lazy fetch and cache
app/server.py   webhook intake, poller, JSON API
app/wsgi.py     gunicorn entrypoint
data/           plates.json, curated_map.json
web/frame.html  the display
```

Endpoints: `/`, `/api/current`, `/plate/<n>`, `/webhook`, `/api/demo`, `/healthz`.

History is mirrored to the cache volume so a restart brings the plates back.
Runs as one gunicorn worker on purpose, since the rotation and poller live in
process memory.
