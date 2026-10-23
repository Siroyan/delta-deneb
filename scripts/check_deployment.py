"""Verify the public Step 1 deployment without AWS credentials or SDKs."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


def fetch(url):
    request = Request(url, headers={"User-Agent": "delta-deneb-step1-check"})
    try:
        result = urlopen(request, timeout=15)
    except HTTPError as error:
        result = error
    with result:
        return Response(result.status, {key.lower(): value for key, value in result.headers.items()}, result.read())


def deployment_outputs(document):
    required = {"SiteUrl", "HealthUrl", "StaticBucketName", "FunctionUrl"}
    candidates = [value for value in document.values() if isinstance(value, dict) and required <= value.keys()]
    if len(candidates) != 1:
        raise ValueError("Expected outputs for exactly one delta-deneb foundation stack")
    return candidates[0]


def check_deployment(outputs, *, request=fetch, stage="dev"):
    results = []

    def expect(url, status, content_type=None):
        result = request(url)
        if result.status != status:
            raise ValueError(f"{url}: expected HTTP {status}, received {result.status}")
        if content_type and content_type not in result.headers.get("content-type", ""):
            raise ValueError(f"{url}: expected {content_type} content")
        return result

    base = outputs["SiteUrl"].rstrip("/")
    index = expect(f"{base}/", 200, "text/html")
    if "ピットモニター" not in index.body.decode("utf-8"):
        raise ValueError("The dashboard index was not served")
    results.append("Dashboard HTML is available")

    module = expect(f"{base}/app.js", 200)
    if not any(kind in module.headers.get("content-type", "") for kind in ("javascript", "ecmascript")):
        raise ValueError("app.js does not have a JavaScript MIME type")
    course = expect(f"{base}/assets/course.json", 200, "application/json")
    if json.loads(course.body).get("id") != "motegi_oval_2025_full_v2":
        raise ValueError("The course asset is missing or unexpected")
    results.append("JavaScript and course assets are available")

    health = expect(outputs["HealthUrl"], 200, "application/json")
    payload = json.loads(health.body)
    if any(payload.get(key) != expected for key, expected in {"status": "ok", "service": "delta-deneb", "stage": stage, "step": 1}.items()):
        raise ValueError("Unexpected health response")
    if "no-store" not in health.headers.get("cache-control", ""):
        raise ValueError("The health response must not be cached")
    results.append("CloudFront -> Lambda health check succeeds without caching")

    missing = expect(f"{base}/api/not-found", 404, "application/json")
    if json.loads(missing.body).get("error", {}).get("code") != "not_found":
        raise ValueError("Unknown API paths must return JSON errors")
    results.append("Unknown API paths return JSON 404s")

    expect(outputs["FunctionUrl"].rstrip("/") + "/api/health", 403)
    results.append("Direct unauthenticated Lambda access is denied")
    bucket = outputs["StaticBucketName"]
    expect(f"https://{bucket}.s3.ap-northeast-1.amazonaws.com/index.html", 403)
    results.append("Direct unauthenticated S3 access is denied")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=Path, default=Path("cdk-outputs.dev.json"))
    parser.add_argument("--stage", choices=("dev", "prod"), default="dev")
    args = parser.parse_args()
    try:
        outputs = deployment_outputs(json.loads(args.outputs.read_text()))
        for result in check_deployment(outputs, stage=args.stage):
            print(f"PASS {result}")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"FAIL {error}\n")


if __name__ == "__main__":
    main()
