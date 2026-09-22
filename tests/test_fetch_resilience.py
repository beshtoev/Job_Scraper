"""fetch() must fail soft: a truncated response cannot abort a source run."""

import http.client

import scrape_jobs


class _Response:
    def __init__(self, body=None, error=None):
        self._body, self._error = body, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._error:
            raise self._error
        return self._body


def _patch(monkeypatch, responses):
    calls = iter(responses)
    monkeypatch.setattr(scrape_jobs, "urlopen", lambda req, timeout: next(calls))
    monkeypatch.setattr(scrape_jobs.time, "sleep", lambda s: None)


def test_fetch_retries_incomplete_read(monkeypatch):
    _patch(monkeypatch, [
        _Response(error=http.client.IncompleteRead(b"partial")),
        _Response(body=b"<html>ok</html>"),
    ])
    assert scrape_jobs.fetch("https://example.test/jobs") == "<html>ok</html>"


def test_fetch_returns_empty_after_repeated_incomplete_reads(monkeypatch):
    _patch(monkeypatch, [_Response(error=http.client.IncompleteRead(b"")) for _ in range(3)])
    assert scrape_jobs.fetch("https://example.test/jobs", retries=2) == ""
