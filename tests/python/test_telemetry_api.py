import json

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber
import pytest

from backend.telemetry import handler as api
from backend.telemetry.store import TelemetryStore


NOW = 1800000000000
TOKEN = "example-viewer-token-used-only-in-tests"


def event(path="latest", params=None, token=TOKEN, method="GET"):
    return {"rawPath": f"/api/telemetry/{path}", "queryStringParameters": params,
            "headers": {} if token is None else {"X-Deneb-Token": token},
            "requestContext": {"http": {"method": method}}}


def item(identifier="one", *, received=NOW - 1000, lap=1, total=1000, lap_time=1000, uptime=121000):
    return {"machine_id": {"S": "delta_machine_alpha"}, "sample_id": {"S": identifier},
            "received_at_ms": {"N": str(received)}, "speed": {"N": "27.125"},
            "average_speed": {"NULL": True}, "lap_number": {"N": str(lap)},
            "total_time_ms": {"N": str(total)}, "lap_time_ms": {"N": str(lap_time)},
            "timestamp_ms": {"N": str(uptime)}, "latitude": {"NULL": True},
            "longitude": {"NULL": True}, "memo": {"S": "private note"}}


def key(identifier="one", received=NOW - 1000):
    return {name: item(identifier, received=received)[name] for name in ("machine_id", "sample_id", "received_at_ms")}


def expected_query(*, limit=1, start=None, end=None, ascending=False, cursor=None):
    result = {"TableName": "example-table", "IndexName": "by-machine-received-at",
              "KeyConditionExpression": "#machine = :machine",
              "ExpressionAttributeNames": {"#machine": "machine_id"},
              "ExpressionAttributeValues": {":machine": {"S": "delta_machine_alpha"}},
              "Limit": limit, "ScanIndexForward": ascending, "ConsistentRead": False}
    if start is not None:
        result["KeyConditionExpression"] += " AND #receipt BETWEEN :start AND :end"
        result["ExpressionAttributeNames"]["#receipt"] = "received_at_ms"
        result["ExpressionAttributeValues"].update({":start": {"N": str(start)}, ":end": {"N": str(end)}})
    if cursor:
        result["ExclusiveStartKey"] = cursor
    return result


@pytest.fixture
def ddb(monkeypatch):
    # Explicit dummy credentials keep all tests independent of local profiles.
    client = boto3.client("dynamodb", region_name="ap-northeast-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    with Stubber(client) as stub:
        monkeypatch.setattr(api, "client", lambda service: client if service == "dynamodb" else pytest.fail("Unexpected secret lookup"))
        monkeypatch.setattr(api, "viewer_token", lambda: TOKEN)
        monkeypatch.setattr(api.time, "time", lambda: NOW / 1000)
        monkeypatch.setenv("TELEMETRY_TABLE", "example-table")
        monkeypatch.setenv("TELEMETRY_INDEX", "by-machine-received-at")
        monkeypatch.setenv("MACHINE_ID", "delta_machine_alpha")
        monkeypatch.setenv("STAGE", "dev")
        yield stub
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("token", [None, "", "wrong", "日本語", "x" * 257])
def test_unauthorized_requests_never_query_the_table(ddb, token):
    result = api.handler(event(token=token), None)
    assert result["statusCode"] == 401
    assert result["headers"]["cache-control"] == "no-store"


def test_health_does_not_require_token_or_access_dependencies(monkeypatch):
    monkeypatch.setattr(api, "client", lambda *_: pytest.fail("Health must not call AWS"))
    result = api.handler({"rawPath": "/api/health", "requestContext": {"http": {"method": "GET"}}}, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["step"] == 2


def test_latest_queries_one_item_in_reverse_receipt_order_and_whitelists_fields(ddb):
    ddb.add_response("query", {"Items": [item()]}, expected_query())
    result = api.handler(event(), None)
    payload = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert payload["received_at_ms"] == NOW - 1000
    assert payload["latest"]["timestamp_ms"] == 121000
    assert payload["latest"]["speed"] == 27.125
    assert payload["latest"]["average_speed"] is None
    assert "private note" not in result["body"]


def test_empty_latest_does_not_report_fetch_time_as_data_freshness(ddb):
    ddb.add_response("query", {"Items": []}, expected_query())
    payload = json.loads(api.handler(event(), None)["body"])
    assert payload["latest"] is None and payload["received_at_ms"] is None
    assert payload["fetched_at_ms"] == NOW


def test_history_pagination_handles_last_evaluated_key_and_fixed_window(ddb):
    start, end = NOW - 10000, NOW
    ddb.add_response("query", {"Items": [item()], "LastEvaluatedKey": key()},
                     expected_query(limit=1, start=start, end=end, ascending=True))
    first = api.handler(event("history", {"from_ms": str(start), "to_ms": str(end), "limit": "1"}), None)
    cursor = json.loads(first["body"])["next_cursor"]
    assert cursor
    ddb.add_response("query", {"Items": [item("two", received=NOW)]},
                     expected_query(limit=1, start=start, end=end, ascending=True, cursor=key()))
    second = api.handler(event("history", {"cursor": cursor, "limit": "1"}), None)
    payload = json.loads(second["body"])
    assert payload["next_cursor"] is None
    assert payload["history"][0]["sample_id"] == "two"
    assert payload["from_ms"] == start and payload["to_ms"] == end


@pytest.mark.parametrize("params", [
    {"limit": "1001"}, {"limit": "-1"}, {"limit": "1.5"}, {"limit": "1,2"},
    {"from_ms": str(NOW - 7200001)}, {"to_ms": str(NOW + 1)},
    {"from_ms": "200", "to_ms": "100"}, {"table": "another"},
    {"machine_id": "another"}, {"token": TOKEN}, {"cursor": "invalid"},
])
def test_unbounded_queries_arbitrary_targets_and_query_tokens_are_rejected(ddb, params):
    assert api.handler(event("history", params), None)["statusCode"] == 400


@pytest.mark.parametrize("modification", ["tampered", "expired", "wrong-machine"])
def test_invalid_cursors_never_reach_dynamodb(ddb, modification):
    payload = {"from_ms": NOW - 10000, "to_ms": NOW, "key": key(), "expires_at_ms": NOW + 60000}
    if modification == "expired":
        payload["expires_at_ms"] = NOW - 1
    elif modification == "wrong-machine":
        payload["key"]["machine_id"] = {"S": "another"}
    cursor = api.cursor_encode(payload, TOKEN)
    if modification == "tampered":
        cursor = "x" + cursor[1:]
    assert api.handler(event("history", {"cursor": cursor}), None)["statusCode"] == 400


def test_dependency_failure_is_retryable_and_does_not_expose_aws_details(ddb, caplog):
    ddb.add_client_error("query", service_error_code="AccessDeniedException", service_message="private-aws-details", expected_params=expected_query())
    result = api.handler(event(), None)
    assert result["statusCode"] == 503 and result["headers"]["retry-after"] == "3"
    assert "private-aws-details" not in result["body"] + caplog.text
    assert TOKEN not in result["body"] + caplog.text


def test_head_preserves_status_without_returning_a_body(ddb):
    ddb.add_response("query", {"Items": []}, expected_query())
    result = api.handler(event(method="HEAD"), None)
    assert result["statusCode"] == 200 and result["body"] == ""


def test_unknown_path_is_404_and_unsupported_method_is_405(ddb):
    assert api.handler(event("missing"), None)["statusCode"] == 404
    assert api.handler(event(method="POST"), None)["statusCode"] == 405


def test_laps_reads_multiple_pages_discards_previous_run_and_never_claims_finish(ddb):
    start, end = NOW - 7200000, NOW
    ddb.add_response("query", {"Items": [
        item("lap2", received=NOW, lap=2, total=300500, lap_time=500, uptime=420500),
        item("lap1", received=NOW - 300000),
    ], "LastEvaluatedKey": key("lap1", NOW - 300000)}, expected_query(limit=1000, start=start, end=end))
    ddb.add_response("query", {"Items": [item("old-run", received=NOW - 600000, lap=7, total=2000000, lap_time=20000, uptime=2120000)]},
                     expected_query(limit=1000, start=start, end=end, cursor=key("lap1", NOW - 300000)))
    result = api.handler(event("laps"), None)
    payload = json.loads(result["body"])
    assert payload["laps"] == [{"lap": 1, "time_ms": 300000, "source": "inferred_transition"}]
    assert not payload["truncated"] and not payload["finish_confirmed"]


def test_invalid_stored_items_are_counted_and_not_returned(ddb):
    bad = item()
    bad.pop("total_time_ms")
    ddb.add_response("query", {"Items": [bad]}, expected_query())
    payload = json.loads(api.handler(event(), None)["body"])
    assert payload["latest"] is None and payload["invalid_items"] == 1


def test_secret_cache_expires_and_refresh_failure_does_not_reuse_old_token(monkeypatch):
    class Secrets:
        calls = 0

        def get_secret_value(self, **_):
            self.calls += 1
            if self.calls > 1:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private"}}, "GetSecretValue")
            return {"SecretString": TOKEN}

    secrets = Secrets()
    clock = [100]
    monkeypatch.setattr(api, "_token_cache", None)
    monkeypatch.setattr(api, "client", lambda _: secrets)
    monkeypatch.setenv("VIEWER_SECRET_ARN", "example-secret")
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    assert api.viewer_token() == TOKEN
    assert api.viewer_token() == TOKEN and secrets.calls == 1
    clock[0] += 61
    with pytest.raises(ClientError):
        api.viewer_token()


def test_laps_item_limit_stops_paging(monkeypatch):
    from backend.telemetry import store as module
    monkeypatch.setattr(module, "MAX_LAP_ITEMS", 1)

    class Client:
        def query(self, **args):
            assert args["Limit"] == 1
            return {"Items": [item()], "LastEvaluatedKey": key()}

    store = TelemetryStore(Client(), table_name="example", index_name="example", machine_id="delta_machine_alpha")
    items, truncated, invalid = store.laps_window(start=NOW - 10000, end=NOW)
    assert len(items) == 1 and truncated and invalid == 0


def test_laps_page_limit_bounds_requests_even_for_large_items(monkeypatch):
    from backend.telemetry import store as module
    monkeypatch.setattr(module, "MAX_LAP_PAGES", 2)

    class Client:
        calls = 0

        def query(self, **args):
            self.calls += 1
            return {"Items": [item(str(self.calls))], "LastEvaluatedKey": key(str(self.calls))}

    client = Client()
    store = TelemetryStore(client, table_name="example", index_name="example", machine_id="delta_machine_alpha")
    items, truncated, _ = store.laps_window(start=NOW - 10000, end=NOW)
    assert len(items) == 2 and truncated and client.calls == 2
