"""Step 3 helpers use synthetic values and loopback HTTP; no AWS credentials."""

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import pytest

from scripts.preview_api import PreviewData, TOKEN, handler_class
from scripts.share_link import share_link


def test_share_link_keeps_the_key_in_fragment_only():
    url = urlsplit(share_link("https://example.cloudfront.net/", "synthetic-example"))
    assert url.scheme == "https"
    assert url.query == ""
    assert parse_qs(url.fragment) == {"token": ["synthetic-example"]}


@pytest.mark.parametrize("url", [
    "http://example.cloudfront.net/", "https://example.org/",
    "https://example.cloudfront.net.evil.test/", "https://user@example.cloudfront.net/",
    "https://example.cloudfront.net/?token=bad", "https://example.cloudfront.net/#old",
])
def test_share_link_rejects_unexpected_destinations(url):
    with pytest.raises(ValueError):
        share_link(url, "synthetic-example")


@pytest.mark.parametrize("token", ["", "secret\nvalue", "secret&value", "x" * 257, None])
def test_invalid_key_errors_do_not_echo_the_key(token):
    with pytest.raises(ValueError, match="Viewer token must contain") as error:
        share_link("https://example.cloudfront.net/", token)
    if token:
        assert token not in str(error.value)


@contextmanager
def preview(scenario="live"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(PreviewData(scenario)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get(base, path, token=TOKEN):
    request = Request(base + path, headers={"X-Deneb-Token": token})
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), json.load(response)


def test_preview_http_contract_pagination_and_run_boundaries():
    with preview() as base:
        assert get(base, "/api/telemetry/latest", "wrong")[0] == 401
        status, headers, body = get(base, "/api/telemetry/latest")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert body["latest"]["machine_id"] == "delta_machine_alpha"
        assert body["received_at_ms"] == body["latest"]["received_at_ms"]
        assert body["latest"]["timestamp_ms"] < body["received_at_ms"]
        _, _, first = get(base, "/api/telemetry/history?limit=2")
        assert len(first["history"]) == 2
        assert first["next_cursor"]
        _, _, second = get(base, "/api/telemetry/history?" + urlencode({"cursor": first["next_cursor"], "limit": 2}))
        assert second["history"][0]["received_at_ms"] > first["history"][-1]["received_at_ms"]
        _, _, laps = get(base, "/api/telemetry/laps")
        assert laps["latest"]["total_time_ms"] < 30 * 60000
        assert len(laps["laps"]) == 4
        assert not laps["finish_confirmed"]


def test_preview_scenarios_and_recovery():
    with preview() as base:
        for scenario in ["gps-missing", "stale", "empty", "error", "unauthorized", "live"]:
            assert get(base, f"/__preview/state?scenario={scenario}")[0] == 200
            status, headers, body = get(base, "/api/telemetry/latest")
            if scenario == "error":
                assert status == 503 and headers["Retry-After"] == "3"
            elif scenario == "unauthorized":
                assert status == 401
            elif scenario == "empty":
                assert status == 200 and body["latest"] is None
            else:
                assert status == 200
                if scenario == "gps-missing":
                    assert body["latest"]["latitude"] is None
                elif scenario == "stale":
                    assert body["fetched_at_ms"] - body["received_at_ms"] >= 30000
                else:
                    assert body["latest"]["latitude"] is not None
        assert get(base, "/__preview/state?scenario=unknown")[0] == 400
        assert get(base, "/api/telemetry/history?from_ms=invalid")[0] == 400
