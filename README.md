# ڕاپۆرتکردنی کێشە — Report a Problem (step 1: form + SQL storage)

فۆرمێکی کوردی (سۆرانی) بۆ ڕاپۆرتکردنی کێشەکانی شار (چاڵ، خڵتەدان، لافاو، هتد)
بە وێنە و/یان دەنگ و شوێنی GPS، پاشەکەوتکراو لە بنکەدراوەیەکی SQL.

A Kurdish (Sorani) form for citizens to report city problems (potholes,
overflowing garbage, flooding, etc.) with a photo and/or a voice recording
plus a location, stored in a SQL database — every report is triaged
instantly by Claude (Anthropic API): category, a 1-5 severity score, a
plain-English summary, and which city department should handle it — and
every report with a location is pinned live on a public map (`map.html`).

Built for the AI Hackathon by Foundation Hub — **October 8–9, 2026**.

## Two ways to submit a report

- **📸 Quick Report (default tab)** — camera-first: the citizen taps one
  button, takes a photo of the problem right there on the street, and the
  app does the rest automatically — live GPS (trustworthy here because
  they're standing at the problem, not filling the form out later at home),
  a reverse-geocoded street/neighborhood name, and an AI-written
  category/severity/summary. No typing required.
- **📝 ڕاپۆرتی تەواو (detailed form)** — the original flow: paste a
  coordinate copied from Google Maps, type or record a description. Useful
  when reporting a problem after the fact (e.g. from a photo taken earlier)
  or when GPS isn't available/accurate.

Both flows land in the same `reports` table and get the same AI triage.

## How a report works

- The citizen does **not** pick a category — there's no dropdown on the
  form. `backend/ai.py` fills the category in automatically (along with
  severity/summary/department) the moment the report is submitted, reading
  the photo + description together with Claude. If no `ANTHROPIC_API_KEY`
  is set, or the call fails for any reason, the report still saves
  normally — it's just left unclassified (`ai_status`: `unavailable` or
  `failed`) and can be triaged later from the dashboard.
- In the detailed form, a photo, a coordinate pasted from Google Maps, and
  a street/neighborhood name are all required, plus either a written
  description or a voice recording (or both). Quick Report only requires
  the photo — location and description are filled in automatically.
- Voice notes are recorded in-browser (`MediaRecorder`) and uploaded as a
  real audio file — nothing is simulated. There is no speech-to-text yet;
  that's a follow-up step once this collection pipeline is proven out.

## Map view (`map.html`)

A public, unauthenticated page — linked from the report form and the
dashboard — showing every report that has a location as a colored pin on
an OpenStreetMap map (via Leaflet.js, no API key needed): color reflects
AI-assigned severity (green → red), gray/blue for not-yet-triaged reports.
Tapping a pin shows the photo, category, severity, department, and
location. Submitting a Quick Report links straight to that report's pin
(`map.html?focus=<id>`). It reads `GET /api/map`, which only returns what a
pin shows (no raw descriptions, voice notes or GPS accuracy), so it's safe
to keep public even when the dashboard data is locked behind `ADMIN_TOKEN`.

## AI triage (`backend/ai.py`)

Every `POST /api/reports` runs the photo + description + neighborhood name
through Claude and stores back a category, a 1-5 severity score, a
one-sentence summary, and a suggested city department — shown to the
citizen immediately after they submit, and on the dashboard for every
report. Two admin endpoints exist for reports that predate this feature or
whose first attempt failed:

- `POST /api/reports/{id}/triage` — (re-)runs AI triage on one report,
  using its already-stored photo/description. There's a "🤖 شیکاری بکە"
  button per row on the dashboard for this.
- `POST /api/reports/triage-all` — runs it over every report that hasn't
  been successfully triaged yet (sequential, so it stays well under rate
  limits). There's a button for this at the top of the dashboard too — use
  it once to backfill AI results onto reports collected before this
  feature existed.

**Setup**: get a key at console.anthropic.com and set `ANTHROPIC_API_KEY`
as an env var (locally, or in the Render dashboard for the deployed app —
`render.yaml` already declares the slot, Render will prompt for it on the
next deploy). Optionally set `ANTHROPIC_MODEL` to override the default
model (`claude-sonnet-5-5`; the old default `claude-sonnet-4-5-20250929`
is deprecated and stops working on 2026-11-30). Photos are shrunk in the
browser before upload (max 1600 px) because the API rejects images over
5 MB. Nothing else needs to change — the feature degrades gracefully with
no key set, so it's safe to deploy either way.

## Project layout

```
backend/
  main.py     FastAPI app: API routes + serves the frontend
  db.py       DB schema + queries (SQLite locally, Postgres in production)
  config.py   Category list (single source of truth) + storage paths
  uploads.py  Checks that uploads really are an image / audio file (by content)
frontend/
  index.html      The public Kurdish report form (Quick Report + detailed)
  map.html        Public map view — every located report pinned by severity
  dashboard.html  Your private view of submitted data (see below)
  common.js       Shared helpers (HTML escaping, safe media URLs)
tests/        pytest suite (see "Run the tests" below)
data/         Created automatically: reports.db + uploads/photos, uploads/audio
Procfile, render.yaml   Deployment config for Render (see "Publish it" below)
```

## Run it locally

Requires Python 3.9+.

```bash
./run.sh
```

(This creates a virtualenv, installs dependencies, and starts the server
at http://localhost:8000 — open that in a browser. Data goes to
`data/reports.db`, a local SQLite file, unless you set `DATABASE_URL`.)

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite uses a throwaway SQLite database and never calls the network.
To run it against Postgres too (what production uses), point
`TEST_DATABASE_URL` at an empty database whose name contains `test` (the
tests wipe the `reports` table, so anything else is refused):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/reports_test python -m pytest
```

GitHub Actions runs both on every push and pull request.

## Your dashboard

Open **`/dashboard.html`** (e.g. `http://localhost:8000/dashboard.html`,
or `https://your-deployed-url/dashboard.html` once published) — a private
page (not linked from the public form) showing: total reports, how many
have a photo/voice note/GPS location, a bar chart of reports by category,
a 14-day submissions sparkline, and a full table of every report with
playable audio, viewable photos, and a Google Maps link for each location.
There's also a "Download CSV" button there (same as `/api/reports/export.csv`).

**Locking it down:** set the `ADMIN_TOKEN` env var (Render dashboard →
Environment) to any long random string. From then on the dashboard data,
the CSV export, the AI re-triage endpoints and the media re-attach endpoint
all require that token: the dashboard asks for it once per browser tab and
sends it as an `X-Admin-Token` header (use the same header with `curl`).
The report form and the map stay public. If `ADMIN_TOKEN` is **not** set
everything is open — anyone with the exact URL can view the dashboard — and
the server logs a warning at startup; that's fine for local development, but
set it before sharing the app widely.

## Publish it (a real public URL)

To let other people actually submit reports from their own phones, the
app needs to run somewhere public — not just on your laptop. Two tiers:

**Quick test (minutes, temporary):** run `./run.sh` and expose it with a
tunnel (`ngrok http 8000`, or `cloudflared tunnel --url http://localhost:8000`
for a no-signup option). Good for testing with a few people right now;
the link dies the moment you close your laptop or stop the tunnel — not
suitable for a multi-week data collection push.

**Actually shareable (stays up, ~15 min setup):** deploy to a free host
with a real database behind it. Recommended combo, using pieces that are
already wired up in this project:

1. **Database — Supabase (free, doesn't expire):**
   Create a free project at supabase.com → Project Settings → Database →
   copy the "Connection string" (URI, "Transaction pooler" mode). That's
   your `DATABASE_URL`.
   *(Render also offers a free Postgres, but it auto-deletes after 30
   days — fine if you only need it through the hackathon, but Supabase's
   free tier just doesn't expire, so it's the safer default.)*

2. **App hosting — Render (free web service):**
   Push this project to a GitHub repo → on render.com choose **New →
   Blueprint**, point it at the repo (it will pick up `render.yaml`
   automatically) → when prompted, set the `DATABASE_URL` env var to the
   Supabase connection string from step 1 → deploy. Render gives you a
   public `https://your-app.onrender.com` URL — that's what you share.

   Free-tier caveats worth knowing: the service spins down after 15
   minutes with no traffic and takes about a minute to wake back up on
   the next visit (a normal free-tier trade-off, not a bug), and its
   local disk is wiped on every restart/redeploy — which is exactly why
   step 1 (a real Postgres via `DATABASE_URL`) matters: without it,
   submitted reports would vanish whenever the service goes idle.

   [Sources: Render free tier docs](https://render.com/docs/free),
   [Is Render Free? (2026)](https://justinmckelvey.com/blog/is-render-free)

Once `DATABASE_URL` is set, `backend/db.py` automatically uses Postgres
instead of the local SQLite file — no other code changes needed.

*Want me to actually do this deployment with you (create the GitHub repo,
walk through Render + Supabase together)? Just say so.*

## API

- `GET  /api/config` — category list (key → Kurdish label). Not used by
  the public form (no category field there); the dashboard uses it to
  show readable labels if/when reports get classified.
- `POST /api/reports` — multipart form: `description`, `lat`, `lng`,
  `location_label`, `photo` (file, required), `audio` (file) — plus either
  `description` or `audio`. `lat`/`lng` must be real coordinates (−90…90,
  −180…180), the photo must really be an image (JPEG/PNG/GIF/WebP/HEIC) and
  the audio really an audio recording — checked from the file's bytes, not
  its name — otherwise the request is rejected (`415`/`422`). Free text is
  capped (description 2000 chars, location name 200). Runs AI triage
  synchronously before responding and returns the created report as JSON,
  including `ai_status`, `severity`, `ai_summary`, `department`.
- `GET  /api/map` — **public**: the located reports with only the fields a
  map pin needs. Powers `map.html`.

These need the `X-Admin-Token` header when `ADMIN_TOKEN` is set:

- `POST /api/reports/{id}/triage` — (re-)runs AI triage on one existing
  report.
- `POST /api/reports/triage-all` — runs AI triage over every
  not-yet-triaged report.
- `PATCH /api/reports/{id}/media` — re-attach a `photo` and/or `audio` file
  to an existing report.
- `GET  /api/reports?limit=&offset=` — list stored reports (most recent
  first; `limit` 1–1000, default 200). Powers the dashboard table.
- `GET  /api/reports/stats` — aggregate counts (total, by category, by
  day, with photo/audio/location, AI-triaged count, average severity, by
  department). Powers the dashboard charts.
- `GET  /api/reports/export.csv` — flat CSV export of everything stored,
  including AI fields (UTF-8 with BOM so Excel shows Kurdish text
  correctly; cells that start with `=`, `+`, `-` or `@` are prefixed with
  `'` so they can't run as spreadsheet formulas).

Also public:

- `GET  /media/{photos|audio}/{filename}` — serves an uploaded file.
- `GET  /api/health` — `{"status": "ok", "reports": <count>}`.

## Database

One `reports` table — SQLite locally by default, or Postgres when
`DATABASE_URL` is set (see "Publish it" above):

| column             | type    | notes                                     |
|--------------------|---------|--------------------------------------------|
| id                 | INTEGER | primary key                                |
| created_at         | TEXT    | ISO 8601 UTC                               |
| category           | TEXT    | nullable — filled in by AI triage, or manually |
| description        | TEXT    | nullable                                   |
| lat, lng           | REAL    | nullable                                   |
| location_accuracy  | REAL    | meters, from the browser Geolocation API   |
| location_label     | TEXT    | street/neighborhood name, required         |
| photo_path         | TEXT    | Supabase Storage URL (or local path for older rows) |
| audio_path         | TEXT    | Supabase Storage URL (or local path for older rows) |
| status             | TEXT    | defaults to `new`                          |
| severity           | INTEGER | 1-5, from AI triage, nullable               |
| ai_summary         | TEXT    | one-sentence AI summary, nullable          |
| department         | TEXT    | AI-suggested city department, nullable     |
| ai_status          | TEXT    | `pending` \| `done` \| `failed` \| `unavailable` |

Inspect the local SQLite file directly any time:

```bash
sqlite3 data/reports.db "select id, created_at, category, lat, lng from reports;"
```

## Known limits / next steps

- No Kurdish speech-to-text yet — AI triage reads the photo + typed
  description, but a voice-only report (no typed description) doesn't get
  its audio transcribed or factored into the triage yet. That's the
  natural next AI step.
- No spam/duplicate detection or rate limiting on the public form (each
  submission costs one AI call). The dashboard is protected only by the
  single shared `ADMIN_TOKEN`, and only when you set it — see "Your
  dashboard" above.
- CORS is wide open (`*`) since the form is meant to be shared broadly for
  a demo; tighten this before any real production use.
- Category and location values only ever need to change in
  `backend/config.py`.
