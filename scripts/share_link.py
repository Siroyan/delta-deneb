"""User-run helper: print the browser share link using the existing viewer key."""

import argparse
from getpass import getpass
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit, urlunsplit

try:
    from scripts.check_deployment import deployment_outputs
    from scripts.check_telemetry import read_viewer_token
except ModuleNotFoundError:
    from check_deployment import deployment_outputs
    from check_telemetry import read_viewer_token


def share_link(site_url, token):
    url = urlsplit(site_url)
    if url.scheme != "https" or not url.hostname or not url.hostname.endswith(".cloudfront.net") or url.username or url.password or url.query or url.fragment:
        raise ValueError("Expected the HTTPS CloudFront SiteUrl from the deployment outputs")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", token):
        raise ValueError("Viewer token must contain only letters, digits, hyphens, and underscores")
    return urlunsplit((url.scheme, url.netloc, url.path or "/", "", urlencode({"token": token})))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=Path, default=Path("cdk-outputs.dev.json"))
    parser.add_argument("--profile", help="Read the viewer key with your own AWS CLI profile")
    args = parser.parse_args()
    try:
        outputs = deployment_outputs(json.loads(args.outputs.read_text()))
        token = read_viewer_token(args.profile, outputs["ViewerSecretArn"]) if args.profile else getpass("Viewer token (input hidden): ")
        print(share_link(outputs["SiteUrl"], token))
    except subprocess.TimeoutExpired:
        parser.exit(1, "FAIL Timed out reading the viewer token\n")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"FAIL {error}\n")


if __name__ == "__main__":
    main()
