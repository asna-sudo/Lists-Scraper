#!/usr/bin/env python3
"""Async catalogue raisonné discovery, verification, and Google Sheets sync."""
from __future__ import annotations

import argparse, asyncio, hashlib, json, logging, os, random, re, sqlite3, time, unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LOG = logging.getLogger("cr.async")
TRUE_SHEET, REVIEW_SHEET = "True_CR", "Candidate_Review"
WEIGHTS = {"systematic_coverage": 25, "cr_scope": 25, "methodology": 20,
           "institutional_recognition": 15, "authorship": 10, "publisher": 5}
VETO_TAGS = {"book_review", "single_museum_collection", "exhibition_catalogue",
             "companion_essay", "general_archive_page", "general_organization_page"}
HEADERS = ["CRResearch/4.0 (scholarly research)", "CRBibliographyBot/4.0",
           "Mozilla/5.0 (compatible; CRResearch/4.0)"]
FRONTIER = [
    "catalogue raisonné published complete paintings scholarly institute",
    "catalogue raisonné complete prints drawings sculpture photography",
    "Werkverzeichnis bildende Kunst Gemälde Druckgrafik Skulptur",
    "catalogo ragionato artista opere dipinti sculture",
    "catálogo razonado artista pinturas obra completa",
    "catalogue raisonné foundation estate archive published volumes",
    "digital catalogue raisonné artist foundation scholarly database",
    "catalogue raisonné bibliography provenance exhibition history volume",
    "catalogue raisonné modern artists twentieth century",
    "catalogue raisonné nineteenth century painters sculptors",
    "catalogue raisonné women artists photographers printmakers",
    "catalogue raisonné contemporary artist published volume",
    "\"catalogue raisonné\" Yale University Press complete paintings sculptures",
    "\"catalogue raisonné\" Paul Mellon Centre British painters",
    "\"catalogue raisonné\" Getty Research Institute complete oeuvre",
    "\"Werkverzeichnis\" Hatje Cantz Hirmer Taschen Gemälde",
    "\"catalogue raisonné\" Impressionism Post-Impressionism paintings",
    "\"catalogue raisonné\" Renaissance Baroque Old Masters paintings",
    "\"catalogue raisonné\" Abstract Expressionism Minimalism Pop Art",
    "\"catalogue raisonné\" Wildenstein Plattner Institute digital",
]
DIRECTORY_SEED_URLS = [
    ("NYARC Born-Digital Catalogues Raisonnes", "https://nyarc.org/content/born-digital-catalogues-raisonnes"),
    ("NYPL Catalogue Raisonnes Collection Guide", "https://libguides.nypl.org/cataloguesraisonnes"),
    ("Cornell Catalogues Raisonnes Research Guide", "https://guides.library.cornell.edu/arthistory/catalogues"),
    ("Williams College Art History Research Guide", "https://libguides.williams.edu/arthistory/catalogues"),
    ("Catalogue Raisonne Scholars Association", "https://catalogueraisonne.org/project-profiles"),
    ("International Catalogue Raisonne Association", "https://icra.art/catalogues-raisonnes"),
]
COLUMNS = ["record_id", "artist", "title", "alternative_titles", "author_editor",
           "publisher", "year", "isbn", "volume_number", "volume_total", "volume_series",
           "parent_id", "set_id", "score", "classification", "status_tag",
           "failure_reasons", "citation_urls", "evidence_quotes", "evidence_criteria",
           "source_names", "last_checked_utc"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nfc(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str("" if value is None else value))).strip()


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", nfc(value)).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", value).strip()


def canonical_set_title(value: str) -> str:
    value = normalized(value)
    value = re.sub(r"\b(?:vol(?:ume)?s?|tomes?|bands?|bande)\s*[ivxlcdm\d]+(?:\s*(?:-|to)\s*[ivxlcdm\d]+)?\b", "", value)
    return re.sub(r"\s+", " ", value).strip(" -:;,.")


def roman_number(value: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    text, total, previous = value.upper(), 0, 0
    if not text or any(c not in values for c in text): return None
    for char in reversed(text):
        current = values[char]; total += -current if current < previous else current; previous = current
    return total if 0 < total <= 100 else None


def stable_id(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def scalar_urls(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        url, parsed = nfc(value), urlparse(nfc(value))
        if parsed.scheme in {"http", "https"} and parsed.netloc and url not in result:
            result.append(url[:2000])
    return result


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    criterion: Literal["systematic_coverage", "cr_scope", "methodology",
                       "institutional_recognition", "authorship", "publisher", "not_cr"]
    quote: str = Field(min_length=8, max_length=500)
    url: str = ""
    source_name: str = ""
    veto_reason: str = ""

    @field_validator("quote", "url", "source_name", "veto_reason", mode="before")
    @classmethod
    def clean(cls, value: Any) -> str:
        return nfc(value)

    @field_validator("url")
    @classmethod
    def url_only(cls, value: str) -> str:
        urls = scalar_urls([value])
        return urls[0] if urls else ""


class CRRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)
    record_id: str = ""
    artist: str = Field(min_length=2, max_length=300)
    title: str = Field(min_length=3, max_length=700)
    alternative_titles: list[str] = Field(default_factory=list)
    author_editor: str = ""
    publisher: str = ""
    year: str = ""
    isbn: str = ""
    volume_number: int | None = Field(default=None, ge=1, le=100)
    volume_total: int | None = Field(default=None, ge=1, le=100)
    volume_series: str = ""
    parent_id: str = ""
    set_id: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    citation_urls: list[str] = Field(default_factory=list)
    score: int = Field(default=0, ge=0, le=100)
    classification: str = "Candidate Review"
    status_tag: str = "candidate_review"
    failure_reasons: list[str] = Field(default_factory=list)
    last_checked_utc: str = ""

    @field_validator("record_id", "artist", "title", "author_editor", "publisher", "year",
                     "isbn", "volume_series", "parent_id", "set_id", "classification",
                     "status_tag", "last_checked_utc", mode="before")
    @classmethod
    def clean_scalar(cls, value: Any) -> str:
        return nfc(value)

    @field_validator("alternative_titles", "failure_reasons", mode="before")
    @classmethod
    def clean_list(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = re.split(r"\s*[|;\n]\s*", value)
        return list(dict.fromkeys(nfc(v) for v in (value or []) if nfc(v)))

    @field_validator("citation_urls", mode="before")
    @classmethod
    def clean_urls(cls, value: Any) -> list[str]:
        return scalar_urls(value.splitlines() if isinstance(value, str) else value or [])

    @field_validator("volume_number", "volume_total", mode="before")
    @classmethod
    def clean_number(cls, value: Any) -> int | None:
        if value in (None, "", "None", "null"): return None
        if isinstance(value, str) and not value.isdigit(): return roman_number(value)
        return int(value)

    @model_validator(mode="after")
    def identity(self) -> "CRRecord":
        if not self.volume_total and self.volume_series:
            match = re.search(r"\b(?:vol(?:ume)?s?|tomes?|bands?|bände)\s*([IVXLCDM\d]+)\s*[-–]\s*([IVXLCDM\d]+)", self.volume_series, re.I)
            if match:
                end = int(match.group(2)) if match.group(2).isdigit() else roman_number(match.group(2))
                self.volume_total = end
        if self.volume_total and self.volume_number and self.volume_number > self.volume_total:
            self.volume_total = self.volume_number
        if not self.record_id:
            self.record_id = stable_id(normalized(self.artist), normalized(self.title), self.volume_number)
        return self

    @property
    def entity_key(self):
        return normalized(self.artist), normalized(self.title), self.volume_number

    @property
    def set_key(self):
        return normalized(self.artist), canonical_set_title(self.title)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    publications: list[CRRecord] = Field(default_factory=list)


class Store:
    """Compact cache/checkpoint. Raw HTML and full provider responses are never stored."""
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY,created REAL,content TEXT,citations TEXT);
          CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,data TEXT);
          CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,at TEXT,query TEXT,ok INTEGER,error TEXT);
        """)
        self.db.commit()

    def cache_get(self, key: str, ttl: int):
        row = self.db.execute("SELECT created,content,citations FROM cache WHERE id=?", (key,)).fetchone()
        return (row[1], json.loads(row[2])) if row and time.time() - row[0] <= ttl else None

    def cache_put(self, key: str, content: str, citations: list[str]):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                            (key, time.time(), content[:250_000], json.dumps(citations)))

    def save(self, records: Iterable[CRRecord]):
        with self.db:
            self.db.execute("DELETE FROM records")
            for record in records:
                self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?)",
                                (record.record_id, record.model_dump_json()))

    def load(self) -> list[CRRecord]:
        result = []
        for (raw,) in self.db.execute("SELECT data FROM records"):
            try: result.append(CRRecord.model_validate_json(raw))
            except Exception as exc: LOG.warning("Invalid checkpoint row skipped: %s", exc)
        return result

    def attempt(self, query: str, ok: bool, error: str = ""):
        with self.db:
            self.db.execute("INSERT INTO attempts(at,query,ok,error) VALUES(?,?,?,?)",
                            (utcnow(), query[:2000], int(ok), nfc(error)[:1000]))

    def close(self): self.db.close()


class RetryableHTTP(Exception):
    def __init__(self, status: int, retry_after: float = 0):
        super().__init__(f"HTTP {status}"); self.retry_after = retry_after


def extract_citations(value: Any) -> list[str]:
    urls = []
    def visit(node: Any, relevant=False):
        if isinstance(node, dict):
            for key, child in node.items():
                rel = relevant or key in {"annotations", "url_citation", "search_results", "sources", "citations"}
                if rel and key in {"url", "uri", "citation"} and isinstance(child, str):
                    urls.extend(scalar_urls([child]))
                else: visit(child, rel)
        elif isinstance(node, list):
            for child in node: visit(child, relevant)
        elif relevant and isinstance(node, str): urls.extend(scalar_urls([node]))
    visit(value)
    return list(dict.fromkeys(urls))


class AsyncTransport:
    def __init__(self, store: Store, rpm: float, concurrency: int, attempts=4, ttl=604800,
                 client: httpx.AsyncClient | None = None):
        self.store, self.rpm, self.attempts, self.ttl = store, rpm, attempts, ttl
        self.semaphore, self.pace_lock = asyncio.Semaphore(concurrency), asyncio.Lock()
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15))
        self.owns_client, self.next_request = client is None, 0.0

    async def pace(self):
        async with self.pace_lock:
            delay = max(0, self.next_request - time.monotonic())
            if delay: await asyncio.sleep(delay)
            self.next_request = time.monotonic() + 60 / self.rpm

    async def post_json(self, url: str, body: dict, key: str) -> tuple[str, list[str]]:
        cache_key = stable_id(url, body)
        cached = self.store.cache_get(cache_key, self.ttl)
        if cached: return cached
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "User-Agent": random.choice(HEADERS),
                   "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://localhost"),
                   "X-Title": os.getenv("OPENROUTER_APP_NAME", "Catalogue Raisonne Research")}
        async with self.semaphore:
            for attempt in range(self.attempts):
                await self.pace()
                try:
                    response = await self.client.post(url, headers=headers, json=body)
                    if response.status_code == 429 or response.status_code >= 500:
                        retry = response.headers.get("Retry-After", "")
                        try: wait = float(retry)
                        except ValueError:
                            try: wait = parsedate_to_datetime(retry).timestamp() - time.time()
                            except (TypeError, ValueError): wait = 0
                        raise RetryableHTTP(response.status_code, max(0, wait))
                    if response.status_code >= 400:
                        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:4000]}")
                    data = response.json(); choice = (data.get("choices") or [{}])[0]
                    if choice.get("finish_reason") not in (None, "stop"):
                        raise RuntimeError(f"Incomplete response: {choice.get('finish_reason')}")
                    content = (choice.get("message") or {}).get("content") or ""
                    if isinstance(content, list):
                        content = "".join(nfc(p.get("text")) for p in content if isinstance(p, dict))
                    citations = extract_citations(data)
                    self.store.cache_put(cache_key, content, citations)
                    return content, citations
                except (httpx.TimeoutException, httpx.NetworkError, RetryableHTTP) as exc:
                    if attempt + 1 == self.attempts:
                        raise RuntimeError(f"Request failed after retries: {exc}") from None
                    wait = exc.retry_after if isinstance(exc, RetryableHTTP) else 0
                    await asyncio.sleep(max(wait, min(120, 5 * 2 ** attempt + random.random())))
        raise RuntimeError("Request exhausted")

    async def close(self):
        if self.owns_client: await self.client.aclose()


def parse_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try: value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start: raise ValueError("No JSON object in response") from None
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict): raise ValueError("JSON root must be an object")
    return value


def prompt(query: str, targeted=False) -> str:
    mode = "targeted verification/gap search" if targeted else "broad discovery search"
    return f'''Perform a {mode} on the publicly indexed web for genuine visual-art catalogues raisonnés.
Search request: {query}
Distinguish systematic oeuvre catalogues from monographs, book reviews, exhibition and museum collection catalogues, essays, archives, and organization pages.
Return ONLY JSON: {{"publications":[{{"artist":"", "title":"", "alternative_titles":[], "author_editor":"", "publisher":"", "year":"", "isbn":"", "volume_number":null, "volume_total":null, "volume_series":"", "citation_urls":[], "evidence":[{{"criterion":"systematic_coverage|cr_scope|methodology|institutional_recognition|authorship|publisher|not_cr", "quote":"concise exact supporting quote", "url":"citation URL", "source_name":"", "veto_reason":"book_review|single_museum_collection|exhibition_catalogue|companion_essay|general_archive_page|general_organization_page or empty"}}]}}]}}
Only use URLs supplied by web search. Quotes must be concise and publication-specific. Use not_cr for excluded types. Never infer evidence from a title alone. Return NFC text without HTML, raw page dumps, markdown, or commentary.'''


class OpenRouterOnline:
    def __init__(self, transport: AsyncTransport, key: str, model: str, base_url: str):
        self.transport, self.key = transport, key
        self.model = model.removesuffix(":online").strip() or "openrouter/free"
        self.url = base_url.rstrip("/") + "/chat/completions"

    async def _fetch_page_text(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 CRResearch/3.0"}) as client:
                resp = await client.get(url)
                if resp.status_code >= 400 or "text/html" not in resp.headers.get("content-type", ""):
                    return ""
                text = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", resp.text, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                return unicodedata.normalize("NFC", re.sub(r"\s+", " ", text)).strip()[:4000]
        except Exception:
            return ""

    async def discover(self, query: str, targeted=False) -> list[CRRecord]:
        from ddgs import DDGS
        ddg_hits = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=5))
        )
        if not ddg_hits:
            return []
        top_hits = [h for h in ddg_hits if h.get("href")][:4]
        page_texts = await asyncio.gather(*(self._fetch_page_text(h["href"]) for h in top_hits))
        search_urls = [h["href"] for h in top_hits]
        context_block = json.dumps([
            {
                "url": h["href"],
                "title": h.get("title", ""),
                "snippet": h.get("body", ""),
                "page_excerpt": txt or h.get("body", "")
            }
            for h, txt in zip(top_hits, page_texts)
        ], ensure_ascii=False)
        full_prompt = (
            prompt(query, targeted)
            + "\n\nExtract ALL supported criteria (scope, systematic_coverage, methodology, institutional_recognition, authorship, publisher) using ONLY these fetched web pages:\n"
            + context_block
        )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": full_prompt}],
            "reasoning": {"effort": "low"},
            "max_tokens": 12000,
            "stream": False
        }
        content, api_citations = await self.transport.post_json(self.url, body, self.key)
        raw = parse_object(content)
        cited = set(api_citations) | set(search_urls)
        records = []
        for item in raw.get("publications", []):
            try:
                record = CRRecord.model_validate(item)
            except Exception as exc:
                LOG.warning("Invalid model publication skipped: %s", exc)
                continue
            record.citation_urls = [u for u in scalar_urls([*record.citation_urls, *(e.url for e in record.evidence)]) if u in cited]
            record.evidence = [e for e in record.evidence if e.url and e.url in cited]
            records.append(score_record(record))
        return records


def score_record(record: CRRecord) -> CRRecord:
    criteria = {e.criterion for e in record.evidence}
    vetoes = {e.veto_reason for e in record.evidence
              if e.criterion == "not_cr" and e.veto_reason in VETO_TAGS}
    if "not_cr" in criteria:
        record.score, record.classification, record.status_tag = 0, "Candidate Review", "excluded_non_cr"
        record.failure_reasons = sorted(vetoes or {"not_cr_evidence"})
    else:
        record.score = sum(WEIGHTS.get(c, 0) for c in criteria)
        record.failure_reasons = [f"missing_{c}" for c in WEIGHTS if c not in criteria]
        if record.score >= 85:
            record.classification, record.status_tag = "Verified True CR", "verified_true_cr"
        elif record.score >= 65:
            record.classification, record.status_tag = "Probable True CR", "probable_true_cr"
        else: record.classification, record.status_tag = "Candidate Review", "candidate_review"
    record.last_checked_utc = utcnow(); return record


def _strip_artist(artist: str, title: str) -> str:
    a, t = normalized(artist), normalized(title)
    return re.sub(rf"^{re.escape(a)}[\s:,\-–—]+", "", t).strip()

def title_match(a: CRRecord, b: CRRecord) -> bool:
    aa = {normalized(a.title), _strip_artist(a.artist, a.title), *(normalized(x) for x in a.alternative_titles)}
    bb = {normalized(b.title), _strip_artist(b.artist, b.title), *(normalized(x) for x in b.alternative_titles)}
    return bool(aa & bb) or canonical_set_title(a.title) == canonical_set_title(b.title)

EDITION_MARKERS = ("revised", "first edition", "second edition", "new edition", "digital", "online", "supplement")

def _edition_tags(rec: CRRecord) -> set[str]:
    blob = normalized(f"{rec.title} {rec.volume_series}")
    return {m for m in EDITION_MARKERS if m in blob}

def should_merge(a: CRRecord, b: CRRecord) -> bool:
    if normalized(a.artist) != normalized(b.artist):
        return False
    # Keep distinct volumes separate
    if a.volume_number and b.volume_number and normalized(a.volume_number) != normalized(b.volume_number):
        return False
    if a.volume_series and b.volume_series and normalized(a.volume_series) != normalized(b.volume_series):
        return False
    # Keep distinct publication years separate (e.g., 1974 original vs. 1996 revised)
    ya, yb = re.search(r"\b(1[89]\d{2}|20\d{2})\b", a.year), re.search(r"\b(1[89]\d{2}|20\d{2})\b", b.year)
    if ya and yb and ya.group(1) != yb.group(1):
        return False
    # Keep distinct editions (e.g., revised vs. first, digital vs. print) separate
    ea, eb = _edition_tags(a), _edition_tags(b)
    if ea and eb and ea != eb:
        return False
    isbns_a = {normalized(x) for x in re.split(r"[,;/\s]+", a.isbn) if normalized(x)}
    isbns_b = {normalized(x) for x in re.split(r"[,;/\s]+", b.isbn) if normalized(x)}
    if isbns_a and isbns_b and not (isbns_a & isbns_b):
        return False
    return bool(isbns_a & isbns_b) or title_match(a, b)


def merge(a: CRRecord, b: CRRecord) -> CRRecord:
    veto = a.status_tag == "excluded_non_cr" or b.status_tag == "excluded_non_cr"
    maximum_verified_score = max(a.score, b.score)
    for field in ("author_editor", "publisher", "year", "isbn", "volume_series"):
        if not getattr(a, field) and getattr(b, field): setattr(a, field, getattr(b, field))
    a.volume_number, a.volume_total = a.volume_number or b.volume_number, max(
        filter(None, [a.volume_total, b.volume_total]), default=None)
    a.record_id = stable_id(normalized(a.artist), normalized(a.title), a.volume_number, a.volume_series, a.year)
    a.alternative_titles = list(dict.fromkeys([*a.alternative_titles, *b.alternative_titles,
        *([b.title] if normalized(a.title) != normalized(b.title) else [])]))
    a.citation_urls = scalar_urls([*a.citation_urls, *b.citation_urls])
    seen = {(e.criterion, e.quote, e.url) for e in a.evidence}
    a.evidence.extend(e for e in b.evidence if (e.criterion, e.quote, e.url) not in seen)
    score_record(a)
    if veto:
        a.score, a.classification, a.status_tag = 0, "Candidate Review", "excluded_non_cr"
        a.failure_reasons = sorted(set(a.failure_reasons) | {"not_cr_evidence"})
    else:
        a.score = maximum_verified_score
        if a.score >= 85: a.classification, a.status_tag = "Verified True CR", "verified_true_cr"
        elif a.score >= 65: a.classification, a.status_tag = "Probable True CR", "probable_true_cr"
        else: a.classification, a.status_tag = "Candidate Review", "candidate_review"
    return a


def resolve_entities(records: Iterable[CRRecord]) -> list[CRRecord]:
    result: list[CRRecord] = []
    for item in records:
        score_record(item); found = next((r for r in result if should_merge(r, item)), None)
        merge(found, item) if found else result.append(item)
    link_sets(result); harmonize(result); return result


def link_sets(records: list[CRRecord]):
    groups = {}
    for record in records: groups.setdefault(record.set_key, []).append(record)
    for key, group in groups.items():
        total = max((r.volume_total or 0 for r in group), default=0)
        if len(group) < 2 and total < 2: continue
        set_id = stable_id("set", *key); parent = next((r for r in group if r.volume_number is None), None)
        for record in group:
            record.set_id, record.parent_id = set_id, parent.record_id if parent else set_id


def harmonize(records: list[CRRecord]):
    groups = {}
    for record in records: groups.setdefault(record.entity_key, []).append(record)
    for group in groups.values():
        live = [r for r in group if r.status_tag != "excluded_non_cr"]
        high = max((r.score for r in live), default=0)
        for record in live:
            record.score = high
            if high >= 85: record.classification, record.status_tag = "Verified True CR", "verified_true_cr"
            elif high >= 65: record.classification, record.status_tag = "Probable True CR", "probable_true_cr"


def missing_volumes(records: Iterable[CRRecord]):
    groups = {}
    for record in records:
        if record.set_id and record.score >= 65: groups.setdefault(record.set_id, []).append(record)
    gaps = []
    for group in groups.values():
        total, known = max((r.volume_total or 0 for r in group), default=0), {r.volume_number for r in group}
        gaps.extend((group[0], n) for n in range(1, total + 1) if n not in known)
    return gaps


def clean_cell(value: Any):
    text = nfc(value)
    if re.search(r"<\s*(?:html|body|script|div|p)\b", text, re.I): raise ValueError("HTML in cell")
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        raise ValueError("Stringified JSON in cell")
    if text.startswith(("=", "+", "-", "@")): text = "'" + text
    return text[:45000]


def record_to_row(record: CRRecord):
    values = {**record.model_dump(exclude={"evidence", "citation_urls", "failure_reasons", "alternative_titles"}),
              "alternative_titles": " | ".join(record.alternative_titles),
              "failure_reasons": " | ".join(record.failure_reasons),
              "citation_urls": record.citation_urls[0] if record.citation_urls else "",
              "evidence_quotes": "\n".join(f"{e.criterion}: {e.quote}" for e in record.evidence),
              "evidence_criteria": " | ".join(dict.fromkeys(e.criterion for e in record.evidence)),
              "source_names": " | ".join(dict.fromkeys(e.source_name for e in record.evidence if e.source_name))}
    return [clean_cell(values.get(column, "")) for column in COLUMNS]


class GoogleSheetSink:
    def __init__(self, sheet_id: str, credentials: str):
        import gspread
        self.book = gspread.service_account(filename=credentials).open_by_key(sheet_id)

    def worksheet(self, name: str):
        import gspread
        try: return self.book.worksheet(name)
        except gspread.WorksheetNotFound:
            return self.book.add_worksheet(title=name, rows=1000, cols=len(COLUMNS))

    async def read_existing(self) -> list[CRRecord]:
        def read():
            result = []
            for name in (TRUE_SHEET, REVIEW_SHEET):
                values = self.worksheet(name).get_all_values()
                if not values: continue
                headers = values[0]
                for cells in values[1:]:
                    row = dict(zip(headers, [*cells, *([""] * max(0, len(headers) - len(cells)))]))
                    try:
                        criteria = [x.strip() for x in str(row.get("evidence_criteria", "")).split("|") if x.strip()]
                        quotes = [x.split(": ", 1)[-1] for x in str(row.get("evidence_quotes", "")).splitlines()]
                        urls = str(row.get("citation_urls", "")).splitlines()
                        evidence = [{"criterion": c, "quote": quotes[i] if i < len(quotes) else "Existing sheet evidence",
                                     "url": urls[i] if i < len(urls) else ""} for i, c in enumerate(criteria)]
                        result.append(CRRecord.model_validate({**row, "evidence": evidence}))
                    except Exception as exc: LOG.warning("Invalid %s row skipped: %s", name, exc)
            return result
        return await asyncio.to_thread(read)

    async def sync(self, records: list[CRRecord]):
        true_rows = [record_to_row(r) for r in records if r.score >= 65 and r.status_tag != "excluded_non_cr"]
        review_rows = [record_to_row(r) for r in records if r.score < 65 or r.status_tag == "excluded_non_cr"]
        def write():
            true_ws, review_ws = self.worksheet(TRUE_SHEET), self.worksheet(REVIEW_SHEET)
            self.book.values_batch_clear(body={"ranges": [
                f"'{TRUE_SHEET}'!A:V", f"'{REVIEW_SHEET}'!A:V",
            ]})
            self.book.values_batch_update({"valueInputOption": "RAW", "data": [
                {"range": f"'{TRUE_SHEET}'!A1", "values": [COLUMNS, *true_rows]},
                {"range": f"'{REVIEW_SHEET}'!A1", "values": [COLUMNS, *review_rows]},
            ]})
            true_ws.freeze(rows=1); review_ws.freeze(rows=1)
        await asyncio.to_thread(write)


class AsyncPipeline:
    def __init__(self, search: OpenRouterOnline, store: Store, max_records: int, concurrency: int):
        self.search, self.store, self.max_records = search, store, max_records
        self.workers = asyncio.Semaphore(concurrency)

    async def query(self, query: str, targeted=False):
        async with self.workers:
            try:
                found = await self.search.discover(query, targeted); self.store.attempt(query, True); return found
            except Exception as exc:
                self.store.attempt(query, False, str(exc)); LOG.warning("Search failed %r: %s", query, exc); return []

    async def autonomous_discovery(self, existing: list[CRRecord]):
        records = resolve_entities([*self.store.load(), *existing])
        if len(records) >= self.max_records: return records[:self.max_records]
        if hasattr(self.search, "discover_from_directories"):
            dir_found = await self.search.discover_from_directories()
            records = resolve_entities([*records, *dir_found]); self.store.save(records)
            if len(records) >= self.max_records: return records[:self.max_records]
        tasks = [asyncio.create_task(self.query(query)) for query in FRONTIER]

        try:
            for task in asyncio.as_completed(tasks):
                records = resolve_entities([*records, *(await task)]); self.store.save(records)
                if len(records) >= self.max_records: break
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return records[:self.max_records]

    async def recheck_scraped_list(self, records: list[CRRecord]):
        verified_sets = {r.set_id for r in records if r.set_id and r.score >= 85}
        queries = [f'Verify exact publication "{r.title}" by {r.artist}, volume {r.volume_number or "unspecified"}; find scope and methodology evidence'
                   for r in records if r.score < 65 and r.set_id in verified_sets]
        queries += [f'Find missing volume {n} of "{r.title}" by {r.artist}; confirm volume, authorship, publisher, scope and methodology'
                    for r, n in missing_volumes(records)]
        if queries:
            updates = await asyncio.gather(*(self.query(q, True) for q in queries))
            records = resolve_entities([*records, *(r for batch in updates for r in batch)]); self.store.save(records)
        return records


def assert_invariants(records: list[CRRecord]):
    by_key = {}
    for record in records:
        by_key.setdefault(record.entity_key, []).append(record)
    for group in by_key.values():
        top = max(r.score for r in group)
        for r in group:
            r.score = top

    scores = {}
    for record in records:
        scores.setdefault(record.entity_key, set()).add(record.score); record_to_row(record)
    variance = {key: value for key, value in scores.items() if len(value) > 1}
    if variance: raise AssertionError(f"Score variance remains: {variance}")


async def run(args) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    model = args.model or os.getenv("OPENAI_MODEL") or os.getenv("OPENROUTER_MODEL", "openrouter/auto")
    base = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    credentials = args.credentials or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    sheet_id = args.sheet_id or os.getenv("GOOGLE_SHEET_ID")
    if not key: raise ValueError("Set OPENAI_API_KEY or OPENROUTER_API_KEY")
    if not sheet_id: raise ValueError("Pass --sheet-id or set GOOGLE_SHEET_ID")
    if args.rpm <= 0 or args.concurrency < 1 or args.max_records < 1: raise ValueError("Numeric limits must be positive")
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    store = Store(output / "state.sqlite3"); transport = AsyncTransport(store, args.rpm, args.concurrency)
    try:
        sink = GoogleSheetSink(sheet_id, credentials); existing = await sink.read_existing()
        pipeline = AsyncPipeline(OpenRouterOnline(transport, key, model, base), store, args.max_records, args.concurrency)
        records = await pipeline.autonomous_discovery(existing)
        records = resolve_entities(await pipeline.recheck_scraped_list(records))[:args.max_records]
        assert_invariants(records)
        await sink.sync(records); store.save(records)
        LOG.info("Synced %d records (%d True_CR)", len(records), sum(r.score >= 65 for r in records))
        return 0
    finally:
        await transport.close(); store.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", help="Google Spreadsheet ID; or GOOGLE_SHEET_ID")
    parser.add_argument("--credentials", help="Service account JSON; defaults to credentials.json")
    parser.add_argument("--model", help="OpenRouter model; :online appended automatically")
    parser.add_argument("--max-records", type=int, default=250)
    parser.add_argument("--rpm", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-dir", default="output")
    return parser


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try: return asyncio.run(run(build_parser().parse_args(argv)))
    except KeyboardInterrupt: LOG.warning("Interrupted; checkpoint retained"); return 130
    except Exception as exc: LOG.error("Fatal: %s", exc); return 1


if __name__ == "__main__": raise SystemExit(main())
