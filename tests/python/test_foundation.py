import json
from pathlib import Path

import aws_cdk as cdk
import pytest

from infra.foundation_stack import FoundationStack


@pytest.fixture(scope="module")
def assembly(tmp_path_factory):
    app = cdk.App(outdir=str(tmp_path_factory.mktemp("cdk")))
    stack = FoundationStack(
        app, "TestFoundation", stage="dev",
        env=cdk.Environment(account="111122223333", region="ap-northeast-1"),
    )
    return app.synth()


@pytest.fixture(scope="module")
def foundation(assembly):
    return assembly.get_stack_artifact("TestFoundation").template


def resources(template, kind):
    return {key: value for key, value in template["Resources"].items() if value["Type"] == kind}


def only(template, kind):
    result = resources(template, kind)
    assert len(result) == 1
    return next(iter(result.items()))


def test_static_bucket_has_no_public_or_website_access_and_is_retained(foundation):
    _, bucket = only(foundation, "AWS::S3::Bucket")
    props = bucket["Properties"]
    assert all(props["PublicAccessBlockConfiguration"].values())
    assert "WebsiteConfiguration" not in props
    assert props["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert bucket["DeletionPolicy"] == "Retain"
    _, policy = only(foundation, "AWS::S3::BucketPolicy")
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    public_statements = [statement for statement in statements if statement.get("Principal") in ("*", {"AWS": "*"})]
    assert public_statements and all(statement["Effect"] == "Deny" for statement in public_statements)
    assert any(statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false" for statement in public_statements)
    reads = [statement for statement in statements if statement["Effect"] == "Allow"]
    assert reads
    assert all(statement["Principal"] == {"Service": "cloudfront.amazonaws.com"} for statement in reads)
    assert all("AWS:SourceArn" in json.dumps(statement["Condition"]) for statement in reads)


def test_s3_and_lambda_origins_are_signed_with_oac(foundation):
    controls = resources(foundation, "AWS::CloudFront::OriginAccessControl")
    assert {value["Properties"]["OriginAccessControlConfig"]["OriginAccessControlOriginType"] for value in controls.values()} == {"s3", "lambda"}
    for value in controls.values():
        config = value["Properties"]["OriginAccessControlConfig"]
        assert config["SigningBehavior"] == "always"
        assert config["SigningProtocol"] == "sigv4"
    _, distribution = only(foundation, "AWS::CloudFront::Distribution")
    origins = distribution["Properties"]["DistributionConfig"]["Origins"]
    assert len(origins) == 2
    assert all("OriginAccessControlId" in origin for origin in origins)


def test_lambda_url_requires_iam_and_both_permissions_are_distribution_scoped(foundation):
    _, url = only(foundation, "AWS::Lambda::Url")
    assert url["Properties"]["AuthType"] == "AWS_IAM"
    assert "Cors" not in url["Properties"]
    permissions = list(resources(foundation, "AWS::Lambda::Permission").values())
    assert {entry["Properties"]["Action"] for entry in permissions} == {"lambda:InvokeFunctionUrl", "lambda:InvokeFunction"}
    distribution_id, _ = only(foundation, "AWS::CloudFront::Distribution")
    for entry in permissions:
        props = entry["Properties"]
        assert props["Principal"] == "cloudfront.amazonaws.com"
        assert distribution_id in json.dumps(props["SourceArn"])
        if props["Action"] == "lambda:InvokeFunction":
            assert props["InvokedViaFunctionUrl"] is True


def test_health_api_does_not_access_dynamodb_or_other_existing_services(foundation):
    assert not any(value["Type"].startswith(("AWS::DynamoDB::", "AWS::IoT::", "AWS::IoTSiteWise::", "AWS::Grafana::")) for value in foundation["Resources"].values())
    api_id, api = next((key, value) for key, value in resources(foundation, "AWS::Lambda::Function").items() if value["Properties"].get("Handler") == "handler.handler")
    assert api["Properties"]["Environment"]["Variables"] == {"STAGE": "dev"}
    role_id = api["Properties"]["Role"]["Fn::GetAtt"][0]
    for policy in resources(foundation, "AWS::IAM::Policy").values():
        if {"Ref": role_id} not in policy["Properties"].get("Roles", []):
            continue
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        for statement in statements:
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            assert all(action.startswith("logs:") for action in actions)
    assert api_id


def test_api_paths_do_not_cache_and_never_fall_back_to_index_html(foundation):
    _, distribution = only(foundation, "AWS::CloudFront::Distribution")
    config = distribution["Properties"]["DistributionConfig"]
    assert config["DefaultRootObject"] == "index.html"
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    behaviors = config["CacheBehaviors"]
    assert {entry["PathPattern"] for entry in behaviors} == {"/api", "/api/*"}
    for entry in behaviors:
        assert entry["AllowedMethods"] == ["GET", "HEAD"]
        assert entry["ViewerProtocolPolicy"] == "https-only"
        assert entry["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # AWS managed CACHING_DISABLED
    assert all(entry["ErrorCachingMinTTL"] == 0 and "ResponsePagePath" not in entry for entry in config["CustomErrorResponses"])
    _, request_policy = only(foundation, "AWS::CloudFront::OriginRequestPolicy")
    request_config = request_policy["Properties"]["OriginRequestPolicyConfig"]
    assert request_config["CookiesConfig"]["CookieBehavior"] == "none"
    assert request_config["HeadersConfig"]["HeaderBehavior"] == "none"


def test_deployment_updates_static_files_and_invalidates_cloudfront(foundation):
    _, deployment = only(foundation, "Custom::CDKBucketDeployment")
    props = deployment["Properties"]
    assert props["DistributionPaths"] == ["/*"]
    assert props["Prune"] is True
    assert props["RetainOnDelete"] is True
    assert props["SystemMetadata"]["cache-control"] == "public,max-age=0,s-maxage=300,must-revalidate"
    bucket_id, _ = only(foundation, "AWS::S3::Bucket")
    assert props["DestinationBucketName"] == {"Ref": bucket_id}


def test_deployment_layer_has_the_name_allowed_by_the_bootstrap(foundation):
    layer_id, layer = only(foundation, "AWS::Lambda::LayerVersion")
    assert layer["Properties"]["LayerName"] == "delta-deneb-dev-site-deployment-awscli"
    functions = resources(foundation, "AWS::Lambda::Function").values()
    assert any({"Ref": layer_id} in function["Properties"].get("Layers", []) for function in functions)


def test_generated_resource_dependencies_have_no_cycles(foundation):
    objects = foundation["Resources"]

    def references(value):
        found = set()
        if isinstance(value, dict):
            if "Ref" in value and value["Ref"] in objects:
                found.add(value["Ref"])
            if "Fn::GetAtt" in value:
                item = value["Fn::GetAtt"]
                name = item[0] if isinstance(item, list) else item.split(".")[0]
                if name in objects:
                    found.add(name)
            for item in value.values():
                found |= references(item)
        elif isinstance(value, list):
            for item in value:
                found |= references(item)
        return found

    visited = set()

    def visit(name, trail):
        assert name not in trail, f"Circular resource dependency: {trail} -> {name}"
        if name in visited:
            return
        resource = objects[name]
        dependencies = resource.get("DependsOn", [])
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        for dependency in references(resource.get("Properties", {})) | set(dependencies):
            visit(dependency, trail | {name})
        visited.add(name)

    for name in objects:
        visit(name, set())


def test_lambda_asset_contains_source_only_not_local_bytecode(assembly):
    root = Path(assembly.directory)
    health_assets = []
    for manifest in root.glob("*.assets.json"):
        for asset in json.loads(manifest.read_text())["files"].values():
            source = root / asset["source"]["path"]
            if source.is_dir() and (source / "handler.py").is_file():
                health_assets.append(source)
    assert len(health_assets) == 1
    assert {path.name for path in health_assets[0].iterdir()} == {"handler.py"}


def test_app_uses_dedicated_bootstrap_and_bounds_every_runtime_role(assembly, foundation):
    properties = json.loads((Path(assembly.directory) / "manifest.json").read_text())["artifacts"]["TestFoundation"]["properties"]
    assert properties["bootstrapStackVersionSsmParameter"] == "/cdk-bootstrap/denebdev1/version"
    assert "cdk-denebdev1-deploy-role-" in properties["assumeRoleArn"]
    assert "hnb659fds" not in json.dumps(json.loads((Path(assembly.directory) / "TestFoundation.assets.json").read_text()))
    for role in resources(foundation, "AWS::IAM::Role").values():
        assert "delta-deneb-runtime-boundary-dev" in json.dumps(role["Properties"]["PermissionsBoundary"])
    _, distribution = only(foundation, "AWS::CloudFront::Distribution")
    assert {tag["Key"]: tag["Value"] for tag in distribution["Properties"]["Tags"]} == {"Project": "delta-deneb", "Environment": "dev"}
