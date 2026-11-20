# Catalogue Raisonné scraper v4

`scraper.py` is an autonomous async discovery and verification pipeline. It starts from a built-in multilingual art-history search frontier, uses OpenRouter online search, resolves publication entities and volume sets, rechecks weak set members and missing volumes, then synchronizes results to Google Sheets. It does not require `--topic` or `--artist`.

## Setup

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set these values in `.env`:

```dotenv
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=your_openrouter_key
OPENAI_MODEL=openrouter/auto
GOOGLE_SHEET_ID=your_spreadsheet_id
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

Share the spreadsheet with the service account's `client_email` as an editor. Keep `.env` and `credentials.json` out of source control.

Run:

```bash
python scraper.py --sheet-id YOUR_SHEET_ID --max-records 250 --rpm 10 --concurrency 4
```

`--sheet-id` may be omitted when `GOOGLE_SHEET_ID` is set. The model gets `:online` automatically. Options:

```text
--credentials PATH       service-account file (default: credentials.json)
--model MODEL            OpenRouter model (default: OPENAI_MODEL/openrouter/auto)
--max-records N          hard synchronized-record cap (default: 250)
--rpm N                  global OpenRouter request rate (default: 10)
--concurrency N          concurrent discovery workers (default: 4)
--output-dir PATH        checkpoint directory (default: output)
```

## Behavior

The bounded multilingual frontier covers published, digital, medium-specific, geographic, period, foundation, estate, and institutional CR references. OpenRouter online search supplies citations. Evidence scores only when its URL appears in provider citation metadata.

Records are NFC-normalized Pydantic models. SQLite stores compact generated JSON content, scalar citations, normalized records, and request outcomes. It does not store HTTP pages, full provider responses, or HTML. Repeated identical requests use the local cache. Interrupted runs retain records in `output/state.sqlite3`.

HTTP calls use `httpx.AsyncClient`, bounded concurrency, global RPM pacing, rotating identifying headers, timeouts, and exponential retry with jitter for 429/5xx responses. `Retry-After` takes precedence. Permanent failures do not create false-negative classifications.

Scoring:

| Criterion | Points |
|---|---:|
| Systematic coverage | 25 |
| CR/Werkverzeichnis scope | 25 |
| Methodology | 20 |
| Institutional recognition | 15 |
| Authorship | 10 |
| Publisher | 5 |

A `not_cr` finding vetoes all points and sets `excluded_non_cr` for book reviews, single-museum collection catalogues, exhibition catalogues, companion essays, and general archive/organization pages. Scores 85–100 are Verified, 65–84 Probable, and lower scores remain in `Candidate_Review` with `missing_*` tags.

Entity resolution uses normalized artist, title aliases, and volume number. Compatible duplicates merge when one lacks volume or ISBN; contradictory known volumes/ISBNs remain separate. Multi-volume records receive stable `set_id` and `parent_id`. Roman/numeric ranges such as `Volumes I–IV` are recognized. Duplicate scores use the maximum evidence score unless a merged record carries a veto.

After discovery, `recheck_scraped_list` searches weak members of verified sets and missing intermediate volumes. Promotion requires new cited evidence and a score of at least 65.

## Google Sheets

The service creates or uses:

- `True_CR`: Verified and Probable records scoring at least 65.
- `Candidate_Review`: lower-scoring and vetoed records with failure reasons.

Existing rows participate in entity resolution. The invariant pass checks zero score variance and rejects HTML or stringified JSON cells. Citations and concise evidence quotes are newline-delimited scalar cells. Both tabs are cleared and written with one `values_batch_update` request in `RAW` mode.

These are managed tabs: every successful run replaces their contents. Keep manual notes in a separate tab.

## Tests

```bash
python -m pytest -q
```

Coverage includes scoring, every veto, NFC normalization, citation filtering, retries/cache, entity merging, set linking, Roman ranges, gaps, recheck promotion, invariants, autonomous discovery, and dual-tab routing.

## Limits

Bounded search cannot prove exhaustiveness. Search availability, citation shape, output quality, quotas, and pricing depend on OpenRouter. The scraper does not parse PDFs or bypass access controls. Expert review remains appropriate for attribution disputes, edition relationships, incomplete historical series, and ambiguous institutional claims.
