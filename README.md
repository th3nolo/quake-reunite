# QuakeReunite

One place to find people after the June 24, 2026 La Guaira (Vargas) twin earthquake in Venezuela.
Missing people, people found in hospitals, and aid collection centers, all de-duplicated into a
single searchable API and 3D map.

Built for the Cerebras and Google DeepMind Gemma 4 hackathon. Gemma 4 31B on Cerebras reads the
sources, keeps the index current, and answers searches in plain language.

## The problem

After the quake the information was scattered across dozens of AI-generated sites, WhatsApp lists,
hospital-list photos, and PDFs. Most were captcha-walled or JavaScript-only, with no open API, and
they assumed good internet and tech literacy. The same person appeared on five lists with slightly
different spellings and cédulas, so a family had to check a dozen places and still could not tell
whether the "José Rodríguez" on one list was the same as on another. QuakeReunite collapses all of
that into one index that a family can search once.

## What it does

* De-duplicates people across every source by cédula and name, with a common-name guard so two
  different "José Rodríguez" never merge without a strong identifier.
* Reads hospital-list photos and PDFs into structured records (Mistral OCR reads the image, Gemma
  structures the text).
* Researches closed external sources on demand when a real family search misses the local index.
* Tracks aid centers (what they need, where, open or full, freshness) and gives directions.
* Serves a 3D affected-buildings map, a 2D map, and an offline search page, plus a plain-language
  question endpoint and a health dashboard.

  <img width="2546" height="1293" alt="image" src="https://github.com/user-attachments/assets/e35866df-3401-4a57-b193-4fd5ba0b21af" />


## How Gemma on Cerebras runs the ingestion pipeline

This is the core of the project. The pipeline is always on, not a one-time import: every
maintenance cycle re-reads the sources, and Gemma 4 31B does the reading at every stage.

### Where Gemma runs

1. Photos and PDFs (multimodal). A hospital posts a photo of a handwritten admissions list. Mistral
   OCR turns the pixels into text, then Gemma 4 turns that text into clean person records (name,
   cédula, age, status, hospital). About 871 hospitalized people entered the index this way; these
   records are what link a "missing" report to a "found in hospital" record.
2. Web pages. Gemma reads the markdown of relief pages and emits person and center records, and
   returns nothing for pages that are only a search form with no data.
3. Multi-person splitting. A single Venezuela Reporta entry often lists several people in one
   free-text field. Gemma splits those into separate records and drops junk entries, on every cycle,
   across every found-person record.
4. The agent that maintains sources. A Gemma loop calls tools: local search, HTTP, a browser
   (scrape and interact), cédula lookup, deriving a child's name from the parents' records, and
   ingest. When a family search misses the local index, Gemma picks which closed source to check,
   types the name into that site's JavaScript-only search box through the browser, reads the result,
   and ingests it. The index grows from family searches, not from bulk enumeration of external
   sources.
5. Natural-language search. The question endpoint sends the query to Gemma to plan a database lookup,
   runs it, then sends the rows back to Gemma to phrase a short, grounded answer in the family's own
   language, using only the records found.
6. External parsing. When a federated source returns a page, Gemma extracts only the people that
   match the query.

### Keeping it live

A maintainer loop runs every ten minutes. Each cycle it pulls new and changed reports from the
Venezuela Reporta API, re-reads the relief pages, re-reads the aid-center source pages through the
agent, re-resolves duplicates, rebuilds household links, regenerates the map, and writes a metrics
snapshot. When a missing person turns up in a hospital,
the next cycle links the hospital record to the missing report by cédula and the status flips from
"por localizar" to "ingresado" on its own, with no manual step.

### Why fast inference is required

The pipeline is inference-bound. One cycle can call Gemma hundreds of times: cleaning found-person
records, splitting multi-person rows, re-structuring pages, parsing external results. On a typical
GPU provider each call takes seconds, so a full cycle would outgrow its ten-minute interval. On
Cerebras the same calls return in under a second:

* A maintenance cycle finishes fast enough to run every ten minutes and still leave the API
  responsive to families searching at the same time.
* The question endpoint answers in roughly 0.8 second end to end (Gemma plans, the SQLite lookup
  runs in about 17 milliseconds, Gemma phrases the answer).
* When the local index misses, the agent checks external sources and still returns in a few
  seconds, while the person waits on the page.

## Stack

Python, FastAPI, SQLite. Gemma 4 31B on Cerebras (text and vision, OpenAI-compatible Chat
Completions). Mistral OCR. Firecrawl for JavaScript and closed sources. MapLibre GL with Microsoft
AI for Good affected-building footprints and terrain. OpenStreetMap and Photon for geocoding.

## Run

```
pip install -r requirements.txt
DATA_DIR=./out uvicorn api.app:app --host 0.0.0.0 --port 8080   # API + maps
python maintainer/loop.py --loop --interval 600                 # keep the index live
```

Open `http://localhost:8080/` (or `/mapa3d`) for the 3D map, `/buscador` for the offline search page, `/salud` for
observability, and `/docs` for the API.

## Deploy

Docker and Dokploy artifacts are in `deploy/`: `Dockerfile`, `docker-compose.dokploy.yml`,
`.env.example`, and `seed-data.sh` (loads a prebuilt index onto the data volume so a fresh deploy
does not have to re-ingest from scratch).

## Privacy

This is real disaster data. No PII and no API keys are in this repository. The resolved database,
source photos, and scraped material are gitignored and mounted at runtime. Example cédulas and names
in the code are fictional. Keys come from environment variables only.

### Public data and offline downloads

The current application deliberately publishes its resolved person index. Keeping the database
and generated files out of Git does **not** make the running service private.

* `/buscador` serves a self-contained HTML snapshot with **every exported person embedded**,
  including full cédulas, names, statuses, hospitals, origins, and source appearances when present.
  One successful request downloads the index; saving that HTML supports offline searching.
  The browser's 80-result display cap limits the visible list, not the data in the file.
* `pipeline/build_db.py` exports the full resolved SQLite person index to
  `DATA_DIR/buscador.html`; the maintainer regenerates it each cycle. The API serves
  `WEB_DIR/buscador.html` (`WEB_DIR` defaults to the database directory). The legacy
  `pipeline/build.py` embeds all people supplied to it and also writes `out/people.json`,
  `out/records.csv`, and `out/revision.md`. These extra files are local outputs, not routes
  exposed by this API. The HTML is a compact snapshot, not a copy of every database column;
  for example, the database exporter omits raw rows and observation text.
* `/persons`, `/persons/{ci}`, and `/review` also expose person data without authentication.
  API result limits bound individual responses; they do not establish confidentiality.

`API_RATE_PER_MIN` (default 120) limits requests per IP in a rolling 60-second window within
each API process, including requests for `/buscador`. It does not limit the number of records
in an HTML response, prevent bulk downloads, or revoke saved copies. Counters reset on restart
and are not shared across workers. Forwarded IP headers require a trusted proxy that overwrites
them and blocks direct access to the app; clients can otherwise choose their throttle identity.
The best-effort audit log records accepted requests (including IP and query string), not offline
use, and log-write failures do not block responses. Treat that log as sensitive too.

The ingestion policy for search-only external registries concerns how records are collected;
it is not a restriction on downloading records once they enter this public index. Deploy this
model only with records intended for public distribution. A restricted-data deployment needs
an explicit redesign of both exports and API access; lowering the request limit is insufficient.

### Verify the public/offline contract

With the application dependencies and `httpx` available in your Python environment, run:

```sh
uv run --no-project python -m unittest discover -s tests -p 'test_public_offline_index.py' -v
```

The tests create a temporary synthetic SQLite database, exercise both exporters and the actual
ASGI application, and verify that a permitted download includes the full index even when the next
request is throttled. They do not ingest sources or contact a live deployment.
