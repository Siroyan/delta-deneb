"""Read-only health check for the Step 1 CloudFront -> Lambda path."""

from datetime import datetime, timezone
import json
import os


HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
}


def handler(event, context):
    """Handle Lambda Function URL payload version 2.0; never log request data."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    headers = dict(HEADERS)

    if path != "/api/health":
        status = 404
        payload = {"error": {"code": "not_found", "message": "Endpoint not found"}}
    elif method not in {"GET", "HEAD"}:
        status = 405
        headers["allow"] = "GET, HEAD"
        payload = {"error": {"code": "method_not_allowed", "message": "Use GET or HEAD"}}
    else:
        status = 200
        payload = {
            "status": "ok",
            "service": "delta-deneb",
            "stage": os.environ.get("STAGE", "dev"),
            "step": 2,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    return {
        "statusCode": status,
        "headers": headers,
        "isBase64Encoded": False,
        "body": "" if method == "HEAD" else json.dumps(payload, separators=(",", ":")),
    }
