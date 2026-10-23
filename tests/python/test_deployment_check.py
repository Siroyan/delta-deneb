import json

import pytest

from scripts.check_deployment import Response, check_deployment, deployment_outputs


OUTPUTS = {
    "SiteUrl": "https://example.cloudfront.net/",
    "HealthUrl": "https://example.cloudfront.net/api/health",
    "FunctionUrl": "https://example.lambda-url.ap-northeast-1.on.aws/",
    "StaticBucketName": "example-site-bucket",
}


def responses():
    return {
        OUTPUTS["SiteUrl"]: Response(200, {"content-type": "text/html"}, "<title>ピットモニター</title>".encode()),
        "https://example.cloudfront.net/app.js": Response(200, {"content-type": "text/javascript"}, b"export {};"),
        "https://example.cloudfront.net/assets/course.json": Response(200, {"content-type": "application/json"}, b'{"id":"motegi_oval_2025_full_v2"}'),
        OUTPUTS["HealthUrl"]: Response(200, {"content-type": "application/json", "cache-control": "no-store"}, b'{"status":"ok","service":"delta-deneb","stage":"dev","step":1}'),
        "https://example.cloudfront.net/api/not-found": Response(404, {"content-type": "application/json"}, b'{"error":{"code":"not_found"}}'),
        OUTPUTS["FunctionUrl"] + "api/health": Response(403, {}, b"Forbidden"),
        "https://example-site-bucket.s3.ap-northeast-1.amazonaws.com/index.html": Response(403, {}, b"AccessDenied"),
    }


def test_checks_work_without_credentials_or_aws_clients():
    actual = responses()
    assert len(check_deployment(OUTPUTS, request=actual.__getitem__)) == 6


@pytest.mark.parametrize("url", [
    OUTPUTS["FunctionUrl"] + "api/health",
    "https://example-site-bucket.s3.ap-northeast-1.amazonaws.com/index.html",
])
def test_checker_rejects_publicly_accessible_origins(url):
    actual = responses()
    actual[url] = Response(200, {}, b"unexpectedly public")
    with pytest.raises(ValueError, match="expected HTTP 403"):
        check_deployment(OUTPUTS, request=actual.__getitem__)


def test_checker_rejects_cached_api_responses():
    actual = responses()
    actual[OUTPUTS["HealthUrl"]].headers["cache-control"] = "public, max-age=300"
    with pytest.raises(ValueError, match="must not be cached"):
        check_deployment(OUTPUTS, request=actual.__getitem__)


def test_checker_rejects_html_fallback_for_api_paths():
    actual = responses()
    actual["https://example.cloudfront.net/api/not-found"] = Response(200, {"content-type": "text/html"}, b"index.html")
    with pytest.raises(ValueError, match="expected HTTP 404"):
        check_deployment(OUTPUTS, request=actual.__getitem__)


def test_outputs_must_identify_one_stack_unambiguously():
    assert deployment_outputs({"DeltaDenebDev": OUTPUTS}) == OUTPUTS
    for value in ({}, {"stack": {}}, {"one": OUTPUTS, "two": OUTPUTS}):
        with pytest.raises(ValueError):
            deployment_outputs(value)
