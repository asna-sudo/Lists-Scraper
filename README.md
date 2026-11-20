# Oeuvre Index Parser (`oeuvre-index-parser`)

An autonomous, zero-cost async pipeline that discovers, verifies, deduplicates, and catalogs scholarly *catalogues raisonnés* of visual artists directly into Google Sheets (`True_CR` and `Candidate_Review`) without requiring manual `--topic` or `--artist` inputs.

## Key Features
- **Zero-Cost Discovery & Extraction:** Pairs free DuckDuckGo search (`ddgs`) and direct crawls of six vetted art-library directories (NYARC, NYPL, Cornell, Williams College, CRSA, and ICRA) with OpenRouter's free router (`openrouter/free`).
- **Clean Webpage Parsing:** Fetches full webpage text via `httpx` and discards raw HTML/JSON dumps before storage.
- **Weighted Scholarly Scoring & Veto:** Scores candidates `0–100` (`>= 65` routed to `True_CR`, `< 65` routed to `Candidate_Review`) and automatically excludes book reviews, single-museum collection catalogues, and library guides.
- **Edition & Volume Separation:** Deduplicates true repeats across runs while keeping distinct editions, volumes, and publication years properly separated and linked (`parent_id` / `set_id`).
- **Self-Healing Verification Loop:** Automatically re-checks borderline set members, searches for missing intermediate volumes, and caches progress locally in SQLite (`output/state.sqlite3`).

## Setup

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install ddgs
cp .env.example .env
```

Configure `.env` with your OpenRouter key and Google Sheet details:

```dotenv
OPENAI_BASE_URL=https://openrouter.ai
OPENAI_API_KEY=your_openrouter_key
OPENAI_MODEL=openrouter/free
OPENROUTER_MODEL=openrouter/free
OPENAI_WEB_SEARCH=off
GOOGLE_SHEET_ID=your_spreadsheet_id
GOOGLE_APPLICATION_CREDENTIALS=credentials.json
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

Share your target Google Sheet with the service account's `client_email` as **Editor**. Keep `.env` and `credentials.json` out of Git (already listed in `.gitignore`).

## Usage

Run a standard daily batch (fits comfortably within OpenRouter's 50 requests/day free tier, which resets daily at 00:00 UTC / 8:00 AM PHT):

```bash
.venv/bin/python scraper.py --max-records 35 --rpm 4
```

Run offline unit tests:

```bash
.venv/bin/pytest
```