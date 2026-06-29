# QuakeReunite

One place to find people after the June 24, 2026 La Guaira (Vargas) twin earthquake in Venezuela.
Missing people, people found in hospitals, and aid collection centers, all de-duplicated into a
single searchable API and 3D map.

Built for the Cerebras and Google DeepMind Gemma 4 hackathon. Gemma 4 31B running on Cerebras is the
engine that reads the messy sources, keeps the index live, and answers families in plain language.

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
* Reads hospital-list photos and PDFs into structured records (Mistral OCR for the pixels, Gemma for
  the structure).
* Researches closed external sources on demand when a real family search misses the local index.
* Tracks aid centers (what they need, where, open or full, freshness) and gives directions.
* Serves a 3D affected-buildings map, a 2D map, and an offline search page, plus a plain-language
  question endpoint and a health dashboard.

  <img width="2546" height="1293" alt="image" src="https://github.com/user-attachments/assets/e35866df-3401-4a57-b193-4fd5ba0b21af" />


## How Gemma on Cerebras powers a live ingestion pipeline

This is the core of the project. It is a high-volume, always-on pipeline, not a one-time import.
Every maintenance cycle re-reads the sources, and Gemma 4 31B does the heavy reading at every stage.
Cerebras is what makes that practical in real time instead of overnight.

### Where Gemma runs

1. Photos and PDFs (multimodal). A hospital posts a photo of a handwritten admissions list. Mistral
   OCR turns the pixels into text, then Gemma 4 turns that text into clean person records (name,
   cédula, age, status, hospital). About 871 hospitalized people entered the index this way, the
   exact cross-source signal that links a "missing" report to a "found in hospital" record.
2. Web pages. Gemma reads the markdown of relief pages and emits person and center records, and
   correctly returns nothing for search-box shells that hold no data.
3. Multi-person splitting. A single Venezuela Reporta entry often crams several people into one
   free-text field. Gemma splits those into separate records and drops junk, on every cycle, across
   the full found-set.
4. The agent that maintains sources. A Gemma loop drives real tools: local search, HTTP, a real
   browser (scrape and interact), cédula lookup, derive-a-child-name-from-the-parents, and ingest.
   For the one person a family asked about, Gemma decides which closed source to check, drives the
   browser to type the name into a JavaScript-only search box, reads the result, and ingests it. The
   index grows from genuine demand, never from enumeration.
5. Natural-language search. The question endpoint sends the query to Gemma to plan a database lookup,
   runs it, then sends the rows back to Gemma to phrase a short, grounded answer in the family's own
   language, using only the records found.
6. External parsing. When a federated source returns a page, Gemma extracts only the people that
   match the query.

### Keeping it live

A maintainer loop runs every ten minutes. Each cycle it pulls new and changed reports from the
Venezuela Reporta API, re-reads the relief pages, re-reads the aid-center source pages through the
agent, re-resolves duplicates, rebuilds household links, regenerates the map, and writes a metrics
snapshot. The effect is an index that tracks reality. When a missing person turns up in a hospital,
the next cycle links the hospital record to the missing report by cédula and the status flips from
"por localizar" to "ingresado" on its own, with no manual step.

### Why Cerebras speed is the enabler

The pipeline is inference-bound. One cycle can call Gemma hundreds of times: cleaning the found-set,
splitting multi-person rows, re-structuring pages, parsing external results. On a typical GPU
provider each call costs seconds, so a full cycle would take a long time and the promise of a live
index would break. On Cerebras the same calls return in well under a second, which is what makes the
whole design possible:

* A maintenance cycle finishes fast enough to run every ten minutes and still leave the API
  responsive to families searching at the same time.
* The question endpoint answers in roughly 0.8 second end to end (Gemma plans, the SQLite lookup
  runs in about 17 milliseconds, Gemma phrases the answer). A family on a weak connection gets a
  real answer while they wait instead of a spinner.
* On-demand research stays interactive. When the local index misses, the agent fans out to external
  sources and still returns in a few seconds, so "search harder" is something a person can wait for.

Fast inference is not a nice-to-have here. The data is messy and high-volume and the freshness
requirement is real, so a continuous Gemma-in-the-loop pipeline is only practical because the model
is fast.

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

Open `http://localhost:8080/mapa3d` for the 3D map, `/` for the offline search page, `/salud` for
observability, and `/docs` for the API.

## Deploy

Docker and Dokploy artifacts are in `deploy/`: `Dockerfile`, `docker-compose.dokploy.yml`,
`.env.example`, and `seed-data.sh` (loads a prebuilt index onto the data volume so a fresh deploy
does not have to re-ingest from scratch).

## Privacy

This is real disaster data. No PII and no API keys are in this repository. The resolved database,
source photos, and scraped material are gitignored and mounted at runtime. Example cédulas and names
in the code are fictional. Keys come from environment variables only.
