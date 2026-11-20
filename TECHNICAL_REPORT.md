# Technical report: async Google Sheets pipeline

The synchronous v3 provider/workbook architecture was replaced with a bounded async pipeline. `httpx.AsyncClient` uses a shared semaphore and global RPM pacer. Transient failures use exponential backoff, jitter, and `Retry-After`; compact completions and citations are cached in SQLite. Pydantic validates and NFC-normalizes records and evidence before checkpointing.

Discovery no longer depends on user topics or artists. A multilingual frontier drives OpenRouter online search, capped by `--max-records`. Only provider-returned citations can support extracted evidence. Raw web HTML, full API responses, and page dumps are discarded. Sheets receive scalar URLs and concise quotes.

Classification implements the requested 25/25/20/15/10/5 weights and hard non-CR veto. Entity resolution merges compatible incomplete duplicates, preserves contradictory volumes/ISBNs, assigns stable set/parent IDs, harmonizes duplicate scores, recognizes Roman volume ranges, and searches verified sets for weak members and missing volumes.

Google Sheets is the output surface. Existing `True_CR` and `Candidate_Review` rows are included in resolution. The final pass rejects score variance, HTML, and stringified JSON. Both tabs are replaced through one values batch update.

Validation: 21 automated tests pass, compilation succeeds, the installed `gspread` batch APIs were inspected, and CLI help loads. Tests are offline. A live OpenRouter/Sheets run was not made because it consumes quota and overwrites the managed tabs. Start with a low `--max-records` smoke run, review both tabs, then increase the cap.

Limits include bounded recall, dependence on provider citation metadata, no PDF extraction, synchronous `gspread` calls delegated to worker threads, and single-process SQLite. Multi-worker scheduling should use a managed database and distributed rate limiter.
