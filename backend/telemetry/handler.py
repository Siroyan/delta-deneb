"""CloudFront-only, token-protected read API for delta-vega telemetry."""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from backend.health.handler import handler as health_handler
from .model import finish_confirmed, lap_summaries
from .store import MAX_WINDOW_MS, TelemetryStore


HEADERS = {"content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff"}
ROUTES = {"/api/telemetry/latest", "/api/telemetry/history", "/api/telemetry/laps"}
CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 2})
LOGGER = logging.getLogger(__name__)
_clients = {}
_token_cache = None


class BadRequest(ValueError):
    pass


def client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service, config=CLIENT_CONFIG)
    return _clients[service]


def viewer_token():
    global _token_cache
    now = time.monotonic()
    if _token_cache is None or now >= _token_cache[0]:
        secret = client("secretsmanager").get_secret_value(SecretId=os.environ["VIEWER_SECRET_ARN"])
        token = secret["SecretString"]
        if not isinstance(token, str) or not token:
            raise ValueError("Viewer secret is empty")
        _token_cache = (now + 60, token)
    return _token_cache[1]


def response(status, payload, *, method="GET", extra_headers=None):
    return {
        "statusCode": status, "headers": HEADERS | (extra_headers or {}),
        "isBase64Encoded": False,
        "body": "" if method == "HEAD" else json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    }


def error(status, code, *, method="GET", extra_headers=None):
    return response(status, {"error": {"code": code}}, method=method, extra_headers=extra_headers)


def integer(value, *, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}", value):
        raise BadRequest("Invalid integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise BadRequest("Integer outside allowed range")
    return result


def cursor_encode(value, token):
    body = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(token.encode(), b"deneb-history-v1:" + body, hashlib.sha256).hexdigest()
    return body.decode() + "." + signature


def cursor_decode(value, token, *, now, machine_id):
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        body, signature = value.split(".")
        expected = hmac.new(token.encode(), b"deneb-history-v1:" + body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            raise ValueError
        payload = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True))
        if not now < payload["expires_at_ms"] <= now + 15 * 60 * 1000:
            raise ValueError
        start, end = payload["from_ms"], payload["to_ms"]
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= now or end - start > MAX_WINDOW_MS:
            raise ValueError
        key = payload["key"]
        if set(key) != {"machine_id", "sample_id", "received_at_ms"} or key["machine_id"] != {"S": machine_id}:
            raise ValueError
        if not isinstance(key["sample_id"].get("S"), str) or len(key["sample_id"]["S"]) > 128:
            raise ValueError
        integer(key["received_at_ms"].get("N"), minimum=start, maximum=end)
        return payload
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BadRequest("Invalid or expired cursor") from None


def window(params, now):
    end = integer(params.get("to_ms", str(now)), minimum=0, maximum=now)
    start = integer(params.get("from_ms", str(max(0, end - MAX_WINDOW_MS))), minimum=0, maximum=end)
    if end - start > MAX_WINDOW_MS:
        raise BadRequest("Window exceeds two hours")
    return start, end


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    if path == "/api/health":
        return health_handler(event, context)
    if path not in ROUTES:
        return error(404, "not_found", method=method)
    if method not in {"GET", "HEAD"}:
        return error(405, "method_not_allowed", method=method, extra_headers={"allow": "GET, HEAD"})
    supplied = {key.lower(): value for key, value in (event.get("headers") or {}).items()}.get("x-deneb-token")
    if not isinstance(supplied, str) or not 1 <= len(supplied) <= 256:
        return error(401, "unauthorized", method=method)
    try:
        token = viewer_token()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return error(401, "unauthorized", method=method)
        params = event.get("queryStringParameters") or {}
        allowed = {"from_ms", "to_ms", "limit", "cursor"} if path.endswith("/history") else {"from_ms", "to_ms"} if path.endswith("/laps") else set()
        if set(params) - allowed:
            raise BadRequest("Unknown parameter")
        now = int(time.time() * 1000)
        store = TelemetryStore(client("dynamodb"), table_name=os.environ["TELEMETRY_TABLE"],
                               index_name=os.environ["TELEMETRY_INDEX"], machine_id=os.environ["MACHINE_ID"])
        if path.endswith("/latest"):
            latest, invalid = store.latest()
            payload = {"latest": latest, "received_at_ms": latest["received_at_ms"] if latest else None,
                       "fetched_at_ms": now, "invalid_items": invalid}
        elif path.endswith("/history"):
            limit = integer(params.get("limit", "300"), minimum=1, maximum=1000)
            key = None
            expiry = now + 15 * 60 * 1000
            if "cursor" in params:
                if "from_ms" in params or "to_ms" in params:
                    raise BadRequest("Cursor already contains the window")
                cursor = cursor_decode(params["cursor"], token, now=now, machine_id=store.machine_id)
                start, end, key, expiry = cursor["from_ms"], cursor["to_ms"], cursor["key"], cursor["expires_at_ms"]
            else:
                start, end = window(params, now)
            items, key, invalid = store.query(limit=limit, start=start, end=end, key=key)
            next_cursor = cursor_encode({"from_ms": start, "to_ms": end, "key": key, "expires_at_ms": expiry}, token) if key else None
            payload = {"history": items, "from_ms": start, "to_ms": end, "next_cursor": next_cursor,
                       "fetched_at_ms": now, "invalid_items": invalid}
        else:
            start, end = window(params, now)
            latest, invalid = store.latest(start=start, end=end)
            payload = {"laps": lap_summaries([latest] if latest else []), "latest": latest,
                       "from_ms": start, "to_ms": end, "truncated": False,
                       "lap_times_available": latest is not None and latest["completed_lap_times_ms"] is not None,
                       "invalid_items": invalid, "fetched_at_ms": now, "finish_confirmed": finish_confirmed(latest)}
        return response(200, payload, method=method)
    except BadRequest:
        return error(400, "invalid_request", method=method)
    except (BotoCoreError, ClientError, KeyError, ValueError):
        # Never log event, headers, raw records, tokens, or AWS error messages.
        LOGGER.warning("Telemetry dependency unavailable")
        return error(503, "temporarily_unavailable", method=method, extra_headers={"retry-after": "3"})
