# BirdFrame

Your BirdNET-Go station hears a bird; this puts the matching Audubon plate on
your TV as a matted print. Fullscreen, no text, no UI — the TV is the frame.

Two portrait plates hang as a pair:

![a pair of portrait plates](docs/frame.png)

A landscape plate fills the mat on its own:

![a single landscape plate](docs/frame-landscape.png)

---

## Quick start

```bash
docker compose up -d --build
```

Then open `http://<docker-host>:8080` in a browser and press **F11**.

Nothing to configure to try it: with no birds detected yet the screen is bare
mat board. Press **`d`** on the page to seed a few demo detections and see the
plates hang.

### Point it at BirdNET-Go

Edit `BIRDNET_URL` in `docker-compose.yml` to your BirdNET-Go address, then
`docker compose up -d`. That's the only required setting.

---

## Two ways to feed it (and why polling is the default)

**Polling — recommended.** BirdFrame asks BirdNET-Go's
`GET /api/v2/detections/recent` for new detections every `POLL_SECONDS`. That
endpoint needs no authentication and returns *every* detection, so the art keeps
changing all day.

**Webhook — optional.** BirdNET-Go can also push to `POST /webhook`. Worth
knowing before you rely on it: BirdNET-Go only fires detection webhooks for
**new species** by default. Its `detection_consumer.go` returns early unless
`event.IsNewSpecies()` is true, so a webhook-only setup shows a cardinal the
first time one turns up and then essentially never changes. Use webhooks for
"notify me about a first-of-year visitor", and polling for the living picture.

To add the webhook anyway, in BirdNET-Go's config:

```yaml
notification:
  push:
    providers:
      - type: webhook
        enabled: true
        name: birdframe
        endpoints:
          - url: "http://birdframe:8080/webhook"
            method: POST
            timeout: 10s
        filter:
          types: ["detection"]
```

The default payload works as-is. BirdFrame reads `scientific_name`, `species`,
`confidence` and `timestamp` from either the top level or `metadata`, and
accepts confidence as `0.93` or `93`.

---

## Settings

All optional except `BIRDNET_URL`.

| Variable | Default | What it does |
|---|---|---|
| `BIRDNET_URL` | *(empty)* | BirdNET-Go base URL. Empty = webhook-only. |
| `POLL_SECONDS` | `60` | How often to check for new detections. |
| `MIN_CONFIDENCE` | `0.65` | Ignore detections below this. |
| `HISTORY_SIZE` | `40` | How many detections stay in the rotation. |
| `SHOW_UNMATCHED` | `false` | Show species that have no verified plate. |
| `CA_BUNDLE` | *(unset)* | PEM for a TLS-inspecting proxy's CA, if your network has one. |
| `VERIFY_TLS` | `true` | Set `false` only as a last resort behind such a proxy. |

Display options go on the URL: `?rotate=120&poll=10` (seconds) and
`?light=-35,40` (see [the bevel](#the-bevel)). `rotate` is how long a hanging
stays up when several birds are in the queue (default 150s).

---

## Where the art comes from

The 435 plates of Audubon's *Birds of America*, via
[nathanbuchar/audubon-bird-plates](https://github.com/nathanbuchar/audubon-bird-plates)
(the ≥2000px [edition](https://github.com/nathanbuchar/audubon-bird-plates-for-supernote),
~1.1MB per plate — plenty for a 4K panel; the full-res set averages 6.5MB).

Plates are fetched **on first sighting of each species** and cached in the
`/cache` volume forever, so the image stays ~190MB and your disk only fills with
birds you actually get. Note the `download` URLs inside that repo's `data.json`
point at `audubon.org` paths that now 404 — BirdFrame fetches from the repo
itself instead.

Plates are in the public domain. Credit line, as the source repo asks:
*Courtesy of the John James Audubon Center at Mill Grove, Montgomery County
Audubon Collection, and Zebra Publishing.*

---

## How species get matched (the interesting problem)

Audubon named these birds in the 1830s, and ornithology has moved on. Only
**~32%** of plate names match a modern eBird common name exactly. So matching on
names alone silently fails for most of your garden.

Fuzzy matching seems like the fix. It isn't — measured on this data it produces
confidently wrong birds:

| Audubon's name | Fuzzy match | Actually is |
|---|---|---|
| Golden-winged Woodpecker | Golden-naped Woodpecker | **Northern Flicker** |
| Le petit caporal | *(Wikipedia)* Napoleon | **Merlin** |
| Indigo Bird | *(Wikipedia)* Viduidae | **Indigo Bunting** |

A wrong bird on the wall is worse than no bird, so BirdFrame doesn't guess. It
resolves in three tiers, all exact:

1. **`data/curated_map.json`** — 217 hand-verified mappings keyed by
   *scientific* name, which is what BirdNET-Go sends and what stays stable.
   Covers the birds a home station actually hears.
2. Exact common-name match against the plate name.
3. Normalised exact match (case, punctuation, `&`/`and`).

Anything unresolved is skipped rather than approximated. To add a species, put
its scientific name in `curated_map.json`:

```json
"Sitta carolinensis": { "plate": 152, "audubon": "White-breasted Black-capped Nuthatch" }
```

Some plates legitimately serve several species (Audubon often drew three
titmice on one sheet), so repeated plate numbers are expected.

---

## Made for a TV

**The TV is the frame.** So nothing draws a frame: the mat board runs to all
four edges, and there is no text, label, or status anywhere on screen. The only
join is the bevel where the mat meets the print.

- **One bevel, nothing else.** A 45° cut through the board, and nothing more.
  See [the bevel](#the-bevel) below for how it's shaded.
- **Two-up or one.** Most plates are portrait (0.82); about 40% are landscape
  (1.46–1.70). Portraits hang as a pair so a 16:9 screen reads as a deliberate
  diptych; a landscape plate fills the mat alone. The page measures each image
  and decides from the real dimensions.
- **The opening is cut to the plate.** Each window takes that image's true
  aspect ratio, so nothing is letterboxed or stretched.
- **Board tone is deliberate.** The plates carry a white paper margin of their
  own, so the mat is a warmer, clearly darker stock — a paler board makes the
  bevel vanish and the mat stops reading as a mat.

### The bevel

Solid rag board is the same colour all the way through, so the cut face is board
colour under different light — not a white core.

Each of the four faces is a **flat plane**, so each takes exactly **one** flat
tone. A gradient across a flat face isn't how light works. The four tones come
from Lambertian shading (`n·l`) of the four face normals, normalised against the
board's own surface so the board keeps its stated colour:

| face | normal | with light from upper left |
|---|---|---|
| top | `(0, +1, +1)` — points **down** | darkest |
| left | `(+1, 0, +1)` — points **right** | dark |
| right | `(−1, 0, +1)` — points **left** | bright |
| bottom | `(0, −1, +1)` — points **up** | brightest |

The cut flares *toward the viewer*, which inverts the intuition: light from above
strikes the **bottom** face and leaves the **top** face in shade — not the other
way round. The corners are real 45° mitres (CSS mitres adjacent border colours,
which is the same joint a mat cutter leaves):

![the bevel at 9x](docs/bevel-detail.png)

The mat is **flush** with the print — a window mat is cut to the same plane as
the paper, so there's nothing to cast a shadow onto the art.

Light direction is configurable: `?light=<azimuth>,<elevation>` in degrees,
default `-35,40` (the conventional gallery raking light). Azimuth `0` is straight
above, negative is from the left; at `?light=0,45` the left and right faces come
out identical, as they should.
- **Burn-in care.** The composition drifts a few pixels over ~23 minutes, and
  plates cross-fade over 2.6s rather than cutting. The page background is the
  board colour, so the drift never exposes an edge.
- Respects `prefers-reduced-motion`; single plate with a tighter board on a phone.

### Displaying it

- **PC → TV over HDMI** (what this was built for): open the URL, press F11.
- **Frame's built-in browser:** works, but some model years dropped the browser.
- **Pi / mini-PC kiosk:**
  ```bash
  chromium-browser --kiosk --noerrdialogs --disable-infobars \
    --disable-features=TranslateUI --incognito http://<docker-host>:8080
  ```

Art Mode is a separate thing: it plays files uploaded to the TV, not a live web
page, so a browser is the right approach for something that changes on its own.

---

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | The wall. |
| `GET /api/current` | Current plate, recent detections, counters. |
| `GET /plate/<n>` | A plate image (cached on first request). |
| `POST /webhook` | Detection intake for BirdNET-Go. |
| `POST /api/demo` | Seed sample detections. |
| `GET /healthz` | Health, cached plate count, match/miss counters. |

`/healthz` counters are the quick way to see if matching is working: a rising
`unmatched` means species worth adding to the curated map. Container logs name
them (`no plate for …`).

---

## Layout

```
app/plates.py   species -> plate resolution, lazy fetch + cache
app/server.py   webhook intake, poller, JSON API
app/wsgi.py     gunicorn entrypoint
data/plates.json       all 435 plates
data/curated_map.json  the hand-verified species mappings
web/frame.html  the wall (single self-contained page)
```

Detection history is mirrored to the cache volume, so a restart brings the
plates back instead of an empty mat.

Runs as a single gunicorn worker on purpose: the gallery and the poller live in
process memory, so multiple workers would each hold a different set of birds.
