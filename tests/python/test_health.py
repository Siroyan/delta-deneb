from datetime import datetime
import json

import pytest

from backend.health.handler import handler


def request(method="GET", path="/api/health"):
    return {"version": "2.0", "rawPath": path, "requestContext": {"http": {"method": method}}}


def test_health_identifies_service_without_exposing_request_or_environment(monkeypatch):
    monkeypatch.setenv("STAGE", "dev")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-appear")
    event = request()
    event["headers"] = {"authorization": "must-not-appear"}
    result = handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {
        "status": "ok", "service": "delta-deneb", "stage": "dev", "step": 1,
        "timestamp": body["timestamp"],
    }
    assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None
    assert result["headers"]["cache-control"] == "no-store"
    assert "must-not-appear" not in result["body"]


def test_head_has_no_body():
    result = handler(request("HEAD"), None)
    assert result["statusCode"] == 200
    assert result["body"] == ""


@pytest.mark.parametrize("path", ["/api", "/api/other", "/api/health/", "/"])
def test_unknown_paths_are_json_404s_not_health_success(path):
    result = handler(request(path=path), None)
    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_unsupported_methods_are_rejected(method):
    result = handler(request(method), None)
    assert result["statusCode"] == 405
    assert result["headers"]["allow"] == "GET, HEAD"


def test_head_unknown_path_keeps_404_without_body():
    result = handler(request("HEAD", "/api/missing"), None)
    assert result["statusCode"] == 404
    assert result["body"] == ""
