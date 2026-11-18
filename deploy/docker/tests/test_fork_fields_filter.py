"""Behavioural tests for the fork's `fields` payload-trimming feature.

The fork exists to trim crawl payloads for LLM context budgets: `fields` on
`/crawl`, `/crawl/stream` and `/execute_js` lets a caller keep only the parts of
a CrawlResult it actually uses. Upstream has no equivalent (upstream closed
PR #1674), so a merge that drops the plumbing disables the fork's only real
feature *without failing any other test* — which is exactly what happened when
`server.py` was taken from upstream wholesale.

Coverage:
  * `api.py` really filters (streaming and non-streaming, filtered and not);
  * a failed crawl stays a failed crawl when `fields` is used;
  * `server.py` forwards the request field into both crawl paths and filters
    `/execute_js` output;
  * adding `fields` does not weaken the auth gate.
"""

import json
import sys
from pathlib import Path

import pytest

# `deploy/docker` holds server.py/api.py and is imported by bare name.
DOCKER_DIR = Path(__file__).resolve().parents[1]
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

from fork_field_filter import _filter_result_fields  # noqa: E402

FULL_RESULT = {
    "url": "https://example.com",
    "success": True,
    "status_code": 200,
    "html": "<html><body>hi</body></html>",
    "markdown": {
        "raw_markdown": "# Example",
        "references_markdown": "[1] ref",
        "fit_markdown": None,
    },
    "media": {"images": [], "videos": [], "audios": []},
    "js_execution_result": {"answer": 4},
}

FAILED_RESULT = {
    "url": "https://broken.example",
    "success": False,
    "status_code": None,
    "error_message": "net::ERR_NAME_NOT_RESOLVED",
    "markdown": None,
}


class _FakeResult:
    """Minimal stand-in for a crawl4ai CrawlResult."""

    def __init__(self, data: dict):
        self._data = data
        self.url = data.get("url")
        self.success = data.get("success")
        self.error_message = data.get("error_message")

    def model_dump(self) -> dict:
        return json.loads(json.dumps(self._data))


class _FakeCrawler:
    crawler_strategy = object()  # not a PDFCrawlerStrategy -> pooled release path

    def __init__(self, results):
        self._results = results
        self.calls = []

    async def arun(self, url=None, config=None, dispatcher=None):
        self.calls.append(url)
        return list(self._results)

    async def arun_many(self, urls=None, config=None, dispatcher=None):
        self.calls.append(urls)
        return list(self._results)


def _patch_pool(monkeypatch, results, server_module=None):
    """Replace the browser pool with a fake crawler (no Chromium, no network).

    `api` imports the pool lazily, so patching `crawler_pool` covers it; `server`
    binds `get_crawler`/`release_crawler` at import time, so pass `server_module`
    to patch those names as well.
    """
    import crawler_pool

    fake = _FakeCrawler(results)

    async def _get_crawler(*args, **kwargs):
        return fake

    async def _release_crawler(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler_pool, "get_crawler", _get_crawler)
    monkeypatch.setattr(crawler_pool, "release_crawler", _release_crawler)
    if server_module is not None:
        monkeypatch.setattr(server_module, "get_crawler", _get_crawler)
        monkeypatch.setattr(server_module, "release_crawler", _release_crawler)
    return fake


def _auth_headers():
    from auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token({'sub': 'test@example.com'})}"}


# ─────────────────────────── unit: the filter itself ───────────────────────────

def test_no_fields_returns_the_result_unchanged():
    assert _filter_result_fields(FULL_RESULT, None) == FULL_RESULT
    assert _filter_result_fields(FULL_RESULT, []) == FULL_RESULT


def test_flat_selection_keeps_only_requested_keys():
    assert _filter_result_fields(FULL_RESULT, ["url", "success"]) == {
        "url": "https://example.com",
        "success": True,
    }


def test_nested_dot_notation_selects_a_single_subfield():
    assert _filter_result_fields(FULL_RESULT, ["markdown.raw_markdown"]) == {
        "markdown": {"raw_markdown": "# Example"}
    }


def test_unknown_fields_are_dropped_not_invented():
    assert _filter_result_fields(FULL_RESULT, ["nope", "url"]) == {
        "url": "https://example.com"
    }


# ─────────────────────── api.py: streaming path applies it ─────────────────────

async def test_stream_results_applies_fields_and_keeps_the_completion_marker():
    from api import stream_results

    async def gen():
        yield _FakeResult(FULL_RESULT)

    # crawler=None: nothing to dispose, so the generator is self-contained.
    chunks = [chunk async for chunk in stream_results(None, gen(), fields=["url"])]

    assert json.loads(chunks[0]) == {"url": "https://example.com"}
    assert json.loads(chunks[-1]) == {"status": "completed"}


async def test_stream_results_without_fields_streams_the_whole_result():
    from api import stream_results

    async def gen():
        yield _FakeResult(FULL_RESULT)

    chunks = [chunk async for chunk in stream_results(None, gen())]
    first = json.loads(chunks[0])

    assert first["markdown"]["raw_markdown"] == "# Example"
    assert first["html"] == FULL_RESULT["html"]
    assert first["js_execution_result"] == {"answer": 4}


# ───────────────────── api.py: non-streaming path applies it ──────────────────

def _no_ssrf_dns(monkeypatch):
    """Skip the destination lookup: these tests must not touch the network."""
    import api

    monkeypatch.setattr(api, "validate_url_destination", lambda url: None)


async def test_handle_crawl_request_filters_the_response(monkeypatch, server_module):
    import api

    _no_ssrf_dns(monkeypatch)
    _patch_pool(monkeypatch, [_FakeResult(FULL_RESULT)])

    response = await api.handle_crawl_request(
        urls=["https://example.com"],
        browser_config={},
        crawler_config={},
        config=server_module.config,
        fields=["url", "success"],
    )

    assert response["results"] == [{"url": "https://example.com", "success": True}]


async def test_handle_crawl_request_without_fields_is_unfiltered(
    monkeypatch, server_module
):
    import api

    _no_ssrf_dns(monkeypatch)
    _patch_pool(monkeypatch, [_FakeResult(FULL_RESULT)])

    response = await api.handle_crawl_request(
        urls=["https://example.com"],
        browser_config={},
        crawler_config={},
        config=server_module.config,
    )

    assert response["results"][0]["markdown"]["raw_markdown"] == "# Example"
    assert response["results"][0]["html"] == FULL_RESULT["html"]


async def test_filtering_does_not_change_failure_handling(monkeypatch, server_module):
    """A failed crawl stays a failed crawl when fields are requested.

    The filter runs *after* success/failure is decided, so trimming must not
    turn a failure into a success (or raise while doing it).
    """
    import api

    _no_ssrf_dns(monkeypatch)
    _patch_pool(monkeypatch, [_FakeResult(FAILED_RESULT)])

    filtered = await api.handle_crawl_request(
        urls=["https://broken.example"],
        browser_config={},
        crawler_config={},
        config=server_module.config,
        fields=["url", "success"],
    )
    assert filtered["results"] == [
        {"url": "https://broken.example", "success": False}
    ]

    full = await api.handle_crawl_request(
        urls=["https://broken.example"],
        browser_config={},
        crawler_config={},
        config=server_module.config,
    )
    assert full["results"][0]["success"] is False
    assert full["results"][0]["error_message"] == "net::ERR_NAME_NOT_RESOLVED"
    # Filtering changes payload contents only, never the response envelope.
    assert set(full) == set(filtered)


# ────────────────── server.py: the request field is forwarded ──────────────────

def test_crawl_endpoint_forwards_fields(stock_client, server_module, monkeypatch):
    """`fields` must reach handle_crawl_request, not be silently dropped."""
    captured = {}

    async def _fake_handle_crawl_request(**kwargs):
        captured.update(kwargs)
        return {"success": True, "results": [{"url": "https://example.com"}]}

    monkeypatch.setattr(
        server_module, "handle_crawl_request", _fake_handle_crawl_request
    )

    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://example.com"], "fields": ["url"]},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    assert captured.get("fields") == ["url"]
    assert r.json()["results"] == [{"url": "https://example.com"}]


def test_stream_endpoint_forwards_fields(stock_client, server_module, monkeypatch):
    """`/crawl/stream` must pass `fields` to stream_results."""
    captured = {}

    async def _fake_handle_stream_crawl_request(**kwargs):
        async def _gen():
            if False:  # pragma: no cover - shape only
                yield None

        return _FakeCrawler([]), _gen(), {}

    async def _fake_stream_results(crawler, gen, fields=None):
        captured["fields"] = fields
        if False:  # pragma: no cover - async-generator shape only
            yield b""

    monkeypatch.setattr(
        server_module, "handle_stream_crawl_request", _fake_handle_stream_crawl_request
    )
    monkeypatch.setattr(server_module, "stream_results", _fake_stream_results)

    r = stock_client.post(
        "/crawl/stream",
        json={"urls": ["https://example.com"], "fields": ["url", "markdown"]},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    assert captured["fields"] == ["url", "markdown"]


def test_execute_js_filters_output(stock_client, server_module, monkeypatch):
    """`/execute_js` output is trimmed by `fields` too."""
    monkeypatch.setattr(server_module, "EXECUTE_JS_ENABLED", True)
    monkeypatch.setattr(server_module, "validate_url_scheme", lambda url: None)
    monkeypatch.setattr(server_module, "validate_webhook_url", lambda url: None)
    _patch_pool(monkeypatch, [_FakeResult(FULL_RESULT)], server_module)

    r = stock_client.post(
        "/execute_js",
        json={
            "url": "https://example.com",
            "scripts": ["1+1"],
            "fields": ["url", "js_execution_result"],
        },
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    assert r.json() == {
        "url": "https://example.com",
        "js_execution_result": {"answer": 4},
    }


def test_execute_js_without_fields_returns_the_full_result(
    stock_client, server_module, monkeypatch
):
    monkeypatch.setattr(server_module, "EXECUTE_JS_ENABLED", True)
    monkeypatch.setattr(server_module, "validate_url_scheme", lambda url: None)
    monkeypatch.setattr(server_module, "validate_webhook_url", lambda url: None)
    _patch_pool(monkeypatch, [_FakeResult(FULL_RESULT)], server_module)

    r = stock_client.post(
        "/execute_js",
        json={"url": "https://example.com", "scripts": ["1+1"]},
        headers=_auth_headers(),
    )

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["html"] == FULL_RESULT["html"]
    assert payload["markdown"]["raw_markdown"] == "# Example"


@pytest.mark.parametrize("path", ["/crawl", "/crawl/stream", "/execute_js"])
def test_endpoints_still_require_auth_with_fields(stock_client, path):
    """Adding `fields` must not open a hole: all three routes stay gated."""
    r = stock_client.post(
        path, json={"urls": ["https://example.com"], "fields": ["url"]}
    )
    assert r.status_code == 401, f"{path} returned {r.status_code} unauthenticated"
