"""User-run Step 2 check; keep the viewer token in memory, never in arguments."""

import argparse
from getpass import getpass
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from scripts.check_deployment import Response, check_deployment, deployment_outputs
except ModuleNotFoundError:
    from check_deployment import Response, check_deployment, deployment_outputs


def fetch(url, token=None):
    headers = {"User-Agent": "delta-deneb-step2-check"}
    if token:
        headers["X-Deneb-Token"] = token
    request = Request(url, headers=headers)
    try:
        result = urlopen(request, timeout=35)
    except HTTPError as failure:
        result = failure
    with result:
        return Response(result.status, {key.lower(): value for key, value in result.headers.items()}, result.read())


def check_telemetry(outputs, token, *, request=fetch, require_data=False):
    base = outputs["SiteUrl"].rstrip("/")

    def get(path, *, authorized=True, expected=200):
        result = request(base + path, token if authorized else None)
        if result.status != expected:
            raise ValueError(f"{path.split('?')[0]}: expected HTTP {expected}, received {result.status}")
        if "application/json" not in result.headers.get("content-type", ""):
            raise ValueError("API did not return JSON")
        if "no-store" not in result.headers.get("cache-control", ""):
            raise ValueError("Telemetry responses must not be cached")
        return json.loads(result.body)

    unauthorized = get("/api/telemetry/latest", authorized=False, expected=401)
    if unauthorized.get("error", {}).get("code") != "unauthorized":
        raise ValueError("Unauthenticated API access was not rejected")
    latest = get("/api/telemetry/latest")
    if latest.get("invalid_items"):
        raise ValueError("Latest stored telemetry has invalid fields")
    sample = latest["latest"]
    params = {"limit": 2}
    if sample:
        if latest["received_at_ms"] != sample["received_at_ms"] or sample["machine_id"] != "delta_machine_alpha":
            raise ValueError("Unexpected latest sample metadata")
        params["to_ms"] = sample["received_at_ms"]
    history = get("/api/telemetry/history?" + urlencode(params))
    if history.get("invalid_items"):
        raise ValueError("History contains invalid stored telemetry")
    if history.get("next_cursor"):
        get("/api/telemetry/history?" + urlencode({"cursor": history["next_cursor"], "limit": 2}))
    lap_params = {key: value for key, value in params.items() if key == "to_ms"}
    laps = get("/api/telemetry/laps" + ("?" + urlencode(lap_params) if lap_params else ""))
    if laps.get("invalid_items") or laps.get("truncated"):
        raise ValueError("Lap history is incomplete; inspect the stored data or narrow the time window")
    if require_data and (sample is None or not history["history"]):
        raise ValueError("API is reachable but no telemetry is stored yet; publish from delta-vega and retry")
    return [
        "Unauthenticated telemetry requests are rejected",
        "Token-authenticated latest, history, and lap APIs respond without caching",
        "Stored telemetry was read successfully" if sample else "WAIT No stored telemetry yet; ingestion is not verified",
    ]


def read_viewer_token(profile, secret_id, *, run=subprocess.run):
    # The bundled CLI already supports aws login. Boto3's login provider needs
    # an additional native CRT dependency, which the Lambda bundle does not use.
    result = run([
        "aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
        "--region", "ap-northeast-1", "--profile", profile,
        "--query", "SecretString", "--output", "text", "--no-cli-pager",
    ], capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise ValueError("Could not read the viewer token; verify the specified profile's login and permissions")
    token = result.stdout.strip()
    if not token or token == "None":
        raise ValueError("Viewer secret did not contain a token")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=Path, default=Path("cdk-outputs.dev.json"))
    parser.add_argument("--profile", help="Read the token using your own AWS profile, without printing it")
    parser.add_argument("--stage", choices=("dev", "prod"), default="dev")
    parser.add_argument("--require-data", action="store_true")
    args = parser.parse_args()
    try:
        outputs = deployment_outputs(json.loads(args.outputs.read_text()))
        for result in check_deployment(outputs, stage=args.stage, step=2):
            print(f"PASS {result}")
        if args.profile:
            token = read_viewer_token(args.profile, outputs["ViewerSecretArn"])
        else:
            token = getpass("Viewer token (input hidden): ")
        if not token:
            raise ValueError("Viewer token is required")
        for result in check_telemetry(outputs, token, require_data=args.require_data):
            print(result if result.startswith("WAIT ") else f"PASS {result}")
    except subprocess.TimeoutExpired:
        parser.exit(1, "FAIL Timed out reading the viewer token\n")
    except (OSError, URLError, ValueError, KeyError) as failure:
        parser.exit(1, f"FAIL {failure}\n")


if __name__ == "__main__":
    main()
