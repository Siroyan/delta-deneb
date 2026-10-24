import json
import subprocess

import pytest

from scripts.check_deployment import Response
from scripts.check_telemetry import check_telemetry, read_viewer_token


TOKEN = "example-token"
OUTPUTS = {"SiteUrl": "https://example.cloudfront.net/"}


def request_factory(*, sample=None, status=200, cache="no-store", incomplete=False):
    def request(url, token):
        if token is None:
            payload, code = {"error": {"code": "unauthorized"}}, 401
        else:
            assert token == TOKEN
            code = status
            if "/latest" in url:
                payload = {"latest": sample, "received_at_ms": sample["received_at_ms"] if sample else None, "invalid_items": 0}
            elif "/history" in url:
                payload = {"history": [sample] if sample else [], "next_cursor": None, "invalid_items": 0}
            else:
                payload = {"laps": [], "invalid_items": 0, "truncated": incomplete}
        return Response(code, {"content-type": "application/json", "cache-control": cache}, json.dumps(payload).encode())
    return request


def test_empty_table_is_reachable_but_not_claimed_as_ingestion_success():
    result = check_telemetry(OUTPUTS, TOKEN, request=request_factory())
    assert result[-1].startswith("WAIT ")
    with pytest.raises(ValueError, match="no telemetry"):
        check_telemetry(OUTPUTS, TOKEN, request=request_factory(), require_data=True)


def test_reading_a_real_sample_is_required_for_completion():
    sample = {"machine_id": "delta_machine_alpha", "received_at_ms": 1800000000000}
    result = check_telemetry(OUTPUTS, TOKEN, request=request_factory(sample=sample), require_data=True)
    assert result[-1] == "Stored telemetry was read successfully"


@pytest.mark.parametrize("options", [{"status": 503}, {"cache": "max-age=300"}, {"incomplete": True}])
def test_checker_rejects_failed_cached_or_incomplete_api_responses(options):
    with pytest.raises(ValueError):
        check_telemetry(OUTPUTS, TOKEN, request=request_factory(**options))


def test_profile_token_lookup_uses_cli_without_echoing_secret(capsys):
    def run(command, **options):
        assert command[:3] == ["aws", "secretsmanager", "get-secret-value"]
        assert "delta-deneb-login" in command and TOKEN not in command
        assert options["capture_output"] is True and options["text"] is True
        return subprocess.CompletedProcess(command, 0, stdout=TOKEN + "\n", stderr="")

    assert read_viewer_token("delta-deneb-login", "example-secret", run=run) == TOKEN
    assert TOKEN not in capsys.readouterr().out


def test_cli_failure_does_not_expose_stdout_or_stderr():
    def run(command, **_):
        return subprocess.CompletedProcess(command, 1, stdout=TOKEN, stderr="private details")

    with pytest.raises(ValueError) as failure:
        read_viewer_token("delta-deneb-login", "example-secret", run=run)
    assert TOKEN not in str(failure.value) and "private details" not in str(failure.value)


def test_checker_detects_an_iot_rule_that_still_drops_the_new_field():
    sample = {"machine_id": "delta_machine_alpha", "received_at_ms": 1800000000000}
    with pytest.raises(ValueError, match="update the IoT rule and firmware"):
        check_telemetry(OUTPUTS, TOKEN, request=request_factory(sample=sample), require_completed_laps=True)
    for times in ([], [362000, 358000], [0] * 100):
        result = check_telemetry(OUTPUTS, TOKEN, request=request_factory(sample=sample | {"completed_lap_times_ms": times}), require_completed_laps=True)
        assert "Device completed-lap array is available" in result
    for times in ("[]", [True], [-1], [1.5], [0] * 101, [2**53]):
        with pytest.raises(ValueError):
            check_telemetry(OUTPUTS, TOKEN, request=request_factory(sample=sample | {"completed_lap_times_ms": times}), require_completed_laps=True)


def test_latest_sample_is_sufficient_even_before_history_index_backfill():
    sample = {"machine_id": "delta_machine_alpha", "received_at_ms": 1800000000000, "completed_lap_times_ms": []}
    delegate = request_factory(sample=sample)
    def request(url, token):
        result = delegate(url, token)
        if "/history" in url:
            return Response(200, result.headers, json.dumps({"history": [], "next_cursor": None, "invalid_items": 0}).encode())
        return result
    assert check_telemetry(OUTPUTS, TOKEN, request=request, require_completed_laps=True)[-1] == "Stored telemetry was read successfully"
