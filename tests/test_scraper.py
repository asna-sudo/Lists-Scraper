import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import scraper as s


def ev(criterion, url="https://source.example/cr", veto=""):
    return s.Evidence(criterion=criterion, quote="Systematic scholarly evidence for this publication.",
                      url=url, veto_reason=veto)


def rec(**values):
    data = {"artist": "Artist One", "title": "Catalogue raisonné", "evidence": []}
    data.update(values)
    return s.CRRecord(**data)


def test_weighted_scoring_and_routes():
    record = rec(evidence=[ev(x) for x in s.WEIGHTS])
    assert s.score_record(record).score == 100
    assert record.classification == "Verified True CR"
    probable = rec(evidence=[ev("systematic_coverage"), ev("cr_scope"), ev("methodology")])
    assert s.score_record(probable).score == 70
    assert probable.status_tag == "probable_true_cr"


@pytest.mark.parametrize("reason", sorted(s.VETO_TAGS))
def test_hard_veto(reason):
    record = rec(evidence=[ev(x) for x in s.WEIGHTS] + [ev("not_cr", veto=reason)])
    s.score_record(record)
    assert record.score == 0 and record.status_tag == "excluded_non_cr"
    assert reason in record.failure_reasons


def test_nfc_models_and_scalar_cells():
    record = rec(artist="Jose\u0301  Smith", citation_urls=["https://a.example", "https://a.example"],
                 evidence=[ev("cr_scope")])
    assert record.artist == "José Smith"
    row = s.record_to_row(record)
    assert not any(str(v).startswith(("{", "[")) for v in row)
    assert s.clean_cell(0) == "0"
    with pytest.raises(ValueError, match="HTML"):
        s.clean_cell("<html>dump</html>")
    with pytest.raises(ValueError, match="JSON"):
        s.clean_cell('{"raw": true}')


def test_entity_resolution_merges_missing_volume_or_isbn():
    a = rec(isbn="", evidence=[ev("cr_scope")])
    b = rec(isbn="9781234567890", volume_number=1, volume_total=2,
            evidence=[ev("systematic_coverage")])
    merged = s.resolve_entities([a, b])
    assert len(merged) == 1
    assert merged[0].isbn == "9781234567890" and merged[0].volume_number == 1
    assert merged[0].record_id == s.stable_id("artist one", "catalogue raisonne", 1)
    assert merged[0].score == 25 and merged[0].set_id


def test_distinct_volumes_link_to_set_and_find_gap():
    records = [
        rec(volume_number=1, volume_total=3, evidence=[ev(x) for x in s.WEIGHTS]),
        rec(volume_number=3, volume_total=3, evidence=[ev(x) for x in s.WEIGHTS]),
    ]
    resolved = s.resolve_entities(records)
    assert len(resolved) == 2 and resolved[0].set_id == resolved[1].set_id
    assert [number for _, number in s.missing_volumes(resolved)] == [2]


def test_veto_survives_duplicate_merge():
    positive = rec(evidence=[ev(x) for x in s.WEIGHTS])
    vetoed = rec(evidence=[ev("not_cr", veto="book_review")])
    result = s.resolve_entities([positive, vetoed])
    assert len(result) == 1 and result[0].score == 0


def test_citations_only_from_metadata():
    payload = {"choices": [{"message": {"content": '{"url":"https://invented.example"}',
                "annotations": [{"type": "url_citation", "url_citation": {
                    "url": "https://cited.example", "title": "Cited"}}]}}]}
    assert s.extract_citations(payload) == ["https://cited.example"]


@pytest.mark.asyncio
async def test_transport_retries_and_caches(tmp_path, monkeypatch):
    store = s.Store(tmp_path / "state.sqlite3")
    response_429 = httpx.Response(429, headers={"Retry-After": "0"}, request=httpx.Request("POST", "https://x"))
    response_ok = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": "{}", "annotations": [{"url_citation": {"url": "https://cited.example"}}]}}]},
        request=httpx.Request("POST", "https://x"))
    client = Mock(); client.post = AsyncMock(side_effect=[response_429, response_ok])
    transport = s.AsyncTransport(store, rpm=100000, concurrency=2, client=client)
    monkeypatch.setattr(s.asyncio, "sleep", AsyncMock())
    first = await transport.post_json("https://x", {"model": "m"}, "secret")
    second = await transport.post_json("https://x", {"model": "m"}, "secret")
    assert first == second and client.post.await_count == 2
    assert store.db.execute("SELECT content FROM cache").fetchone()[0] == "{}"
    store.close()


@pytest.mark.asyncio
async def test_online_model_payload_and_evidence_filter(tmp_path):
    store = s.Store(tmp_path / "state.sqlite3")
    transport = Mock()
    transport.post_json = AsyncMock(return_value=(json.dumps({"publications": [{
        "artist": "Artist", "title": "Catalogue", "evidence": [
            {"criterion": "cr_scope", "quote": "Long enough genuine cited quote", "url": "https://cited.example"},
            {"criterion": "methodology", "quote": "Long enough invented source quote", "url": "https://invented.example"}
        ]}]}), ["https://cited.example"]))
    client = s.OpenRouterOnline(transport, "key", "vendor/model", "https://openrouter.ai/api/v1")
    records = await client.discover("query")
    body = transport.post_json.call_args.args[1]
    assert body["model"] == "vendor/model:online"
    assert "plugins" not in body
    assert records[0].score == 25 and len(records[0].evidence) == 1
    store.close()


@pytest.mark.asyncio
async def test_invalid_model_item_does_not_discard_valid_item(tmp_path):
    transport = Mock()
    transport.post_json = AsyncMock(return_value=(json.dumps({"publications": [
        {"artist": "", "title": "bad"},
        {"artist": "Valid Artist", "title": "Valid Catalogue", "evidence": []},
    ]}), ["https://cited.example"]))
    client = s.OpenRouterOnline(transport, "key", "model", "https://openrouter.ai/api/v1")
    records = await client.discover("query")
    assert [r.artist for r in records] == ["Valid Artist"]


def test_blank_sheet_volume_numbers_parse():
    record = s.CRRecord.model_validate({"artist": "Artist", "title": "Catalogue",
                                        "volume_number": "", "volume_total": "IV"})
    assert record.volume_number is None and record.volume_total == 4


@pytest.mark.asyncio
async def test_autonomous_pipeline_requires_no_seed(tmp_path, monkeypatch):
    store = s.Store(tmp_path / "state.sqlite3")
    search = Mock()
    search.discover = AsyncMock(return_value=[rec()])
    pipeline = s.AsyncPipeline(search, store, max_records=1, concurrency=2)
    result = await pipeline.autonomous_discovery([])
    assert len(result) == 1 and search.discover.await_count >= 1
    assert store.load()
    store.close()


@pytest.mark.asyncio
async def test_recheck_promotes_candidate_and_fills_gap(tmp_path):
    store = s.Store(tmp_path / "state.sqlite3")
    verified = rec(volume_number=1, volume_total=2, evidence=[ev(x) for x in s.WEIGHTS])
    candidate = rec(volume_number=None, volume_total=2, evidence=[ev("authorship")])
    initial = s.resolve_entities([verified, candidate])
    search = Mock()
    search.discover = AsyncMock(return_value=[rec(volume_number=2, volume_total=2,
        evidence=[ev("systematic_coverage"), ev("cr_scope"), ev("methodology")])])
    pipeline = s.AsyncPipeline(search, store, max_records=10, concurrency=2)
    result = await pipeline.recheck_scraped_list(initial)
    assert any(r.volume_number == 2 and r.score >= 65 for r in result)
    store.close()


def test_invariants_detect_score_variance(monkeypatch):
    a, b = rec(), rec()
    a.score, b.score = 10, 20
    with pytest.raises(AssertionError, match="variance"):
        s.assert_invariants([a, b])


@pytest.mark.asyncio
async def test_sheet_dual_route_batch_update(monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(s.asyncio, "to_thread", inline)
    sink = object.__new__(s.GoogleSheetSink)
    true_ws, review_ws = Mock(), Mock()
    sink.book = Mock()
    sink.worksheet = Mock(side_effect=lambda name: true_ws if name == s.TRUE_SHEET else review_ws)
    verified = s.score_record(rec(evidence=[ev(x) for x in s.WEIGHTS]))
    excluded = s.score_record(rec(title="Review", evidence=[ev("not_cr", veto="book_review")]))
    await sink.sync([verified, excluded])
    body = sink.book.values_batch_update.call_args.args[0]
    assert sink.book.values_batch_clear.call_args.kwargs["body"]["ranges"] == [
        "'True_CR'!A:V", "'Candidate_Review'!A:V"]
    assert len(body["data"]) == 2
    assert body["data"][0]["values"][0] == s.COLUMNS
    assert len(body["data"][0]["values"]) == 2
    assert len(body["data"][1]["values"]) == 2


def test_roman_volume_range_links_and_finds_gaps():
    first = rec(title="Catalogue, Volume I", volume_number=1, volume_series="Volumes I–IV",
                evidence=[ev(x) for x in s.WEIGHTS])
    fourth = rec(title="Catalogue, Volume IV", volume_number=4, volume_series="Volumes I–IV",
                 evidence=[ev(x) for x in s.WEIGHTS])
    records = s.resolve_entities([first, fourth])
    assert records[0].volume_total == 4 and records[0].set_id == records[1].set_id
    assert [n for _, n in s.missing_volumes(records)] == [2, 3]
