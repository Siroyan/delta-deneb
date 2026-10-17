import fnmatch
import json

import pytest
import yaml

from scripts.prepare_bootstrap import BASE_TEMPLATE, build_template


@pytest.fixture(scope="module")
def bootstrap():
    return build_template(yaml.safe_load(BASE_TEMPLATE.read_text()))


def policy(template, name):
    return template["Resources"][name]["Properties"]["PolicyDocument"]["Statement"]


def statements_with_action(statements, action):
    return [item for item in statements if any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in item["Action"])]


def resolved_policy(value, stage="dev"):
    """IAM quotas apply after CloudFormation resolves intrinsic functions."""
    if isinstance(value, list):
        return [resolved_policy(item, stage) for item in value]
    if isinstance(value, dict):
        if "Fn::Sub" in value:
            result = value["Fn::Sub"]
            qualifier = "denebdev1" if stage == "dev" else "denebprd1"
            for key, replacement in {
                "AWS::Partition": "aws", "AWS::Region": "ap-northeast-1", "AWS::AccountId": "111122223333",
                "Qualifier": qualifier,
                "StagingBucket.Arn": f"arn:aws:s3:::cdk-{qualifier}-assets-111122223333-ap-northeast-1",
            }.items():
                result = result.replace("${" + key + "}", replacement)
            assert "${" not in result
            return result
        return {key: resolved_policy(item, stage) for key, item in value.items()}
    return value


def test_bootstrap_never_creates_users_keys_or_administrator_access(bootstrap):
    assert "AdministratorAccess" not in json.dumps(bootstrap)
    assert not any(resource["Type"] in {"AWS::IAM::User", "AWS::IAM::AccessKey", "AWS::IAM::LoginProfile"} for resource in bootstrap["Resources"].values())
    assert bootstrap["Parameters"]["Qualifier"]["AllowedValues"] == ["denebdev1"]
    assert "TrustedAccounts" not in bootstrap["Parameters"]
    assert "CloudFormationExecutionPolicies" not in bootstrap["Parameters"]
    assert bootstrap["Parameters"]["BootstrapVariant"]["AllowedValues"] == ["delta-deneb restricted bootstrap v1"]
    for resource in bootstrap["Resources"].values():
        if resource["Type"] == "AWS::IAM::ManagedPolicy":
            assert len(json.dumps(resolved_policy(resource["Properties"]["PolicyDocument"]), separators=(",", ":"))) <= 6144


def test_bootstrap_roles_only_trust_the_named_user_and_do_not_trust_other_accounts(bootstrap):
    for name in ["DeploymentActionRole", "FilePublishingRole", "ImagePublishingRole", "LookupRole"]:
        statements = bootstrap["Resources"][name]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert len(statements) == 1
        trust = statements[0]
        assert trust["Condition"]["ArnEquals"]["aws:PrincipalArn"] == {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:user/delta-deneb-deployer"}
        assert trust["Condition"]["Null"] == {"sts:ExternalId": "true"}


def test_user_can_only_assume_publishing_deployment_and_limited_lookup_roles(bootstrap):
    props = bootstrap["Resources"]["DeployerUserPolicy"]["Properties"]
    assert props["Users"] == ["delta-deneb-deployer"]
    assume = statements_with_action(props["PolicyDocument"]["Statement"], "sts:AssumeRole")
    assert len(assume) == 1
    assert assume[0]["Resource"] == [{"Fn::Sub": "${DeploymentActionRole.Arn}"}, {"Fn::Sub": "${FilePublishingRole.Arn}"}, {"Fn::Sub": "${LookupRole.Arn}"}]


def test_deploy_role_cannot_modify_toolkit_or_unrelated_stacks(bootstrap):
    props = bootstrap["Resources"]["DeploymentActionRole"]["Properties"]
    assert "ManagedPolicyArns" not in props
    statements = props["Policies"][0]["PolicyDocument"]["Statement"]
    for action in ["cloudformation:CreateChangeSet", "cloudformation:ExecuteChangeSet", "cloudformation:DeleteStack"]:
        grants = statements_with_action(statements, action)
        assert len(grants) == 1
        assert grants[0]["Resource"] == [{"Fn::Sub": "arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/delta-deneb-dev/*"}]
    assert statements_with_action(statements, "iam:PassRole")[0]["Resource"] == [{"Fn::Sub": "${CloudFormationExecutionRole.Arn}"}]


def test_cloudformation_cannot_create_unbounded_roles_or_alter_guardrails(bootstrap):
    props = bootstrap["Resources"]["CloudFormationExecutionRole"]["Properties"]
    assert props["PermissionsBoundary"] == {"Ref": "CloudFormationExecutionBoundary"}
    assert "ManagedPolicyArns" not in props
    statements = policy(bootstrap, "CloudFormationExecutionBoundary")
    allows = [item for item in statements_with_action(statements, "iam:CreateRole") if item["Effect"] == "Allow"]
    assert len(allows) == 1
    assert allows[0]["Resource"] == [{"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/delta-deneb-dev-*"}]
    assert allows[0]["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/delta-deneb-runtime-boundary-dev"}
    for action in ["iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "sts:AssumeRole"]:
        assert any(item["Effect"] == "Deny" and item["Resource"] == "*" for item in statements_with_action(statements, action))
    assert not any(item["Effect"] == "Allow" for item in statements_with_action(statements, "iam:CreateUser"))
    version_read, = statements_with_action(statements, "ssm:GetParameters")
    assert version_read["Resource"] == [{"Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/cdk-bootstrap/${Qualifier}/version"}]


def test_runtime_boundary_blocks_privilege_escalation_even_if_role_policy_is_broadened(bootstrap):
    statements = policy(bootstrap, "ApplicationRuntimeBoundary")
    for action in ["iam:CreateRole", "iam:PutRolePolicy", "sts:AssumeRole", "organizations:CreateAccount"]:
        assert any(item["Effect"] == "Deny" for item in statements_with_action(statements, action))
    for action in ["s3:PutObject", "logs:PutLogEvents"]:
        assert statements_with_action(statements, action)
    assert not statements_with_action(statements, "dynamodb:Scan")


@pytest.mark.parametrize("stage", ["dev", "prod"])
def test_only_the_named_deployment_layer_can_be_published_read_and_deleted(stage):
    template = build_template(yaml.safe_load(BASE_TEMPLATE.read_text()), stage=stage)
    layer = f"arn:${{AWS::Partition}}:lambda:${{AWS::Region}}:${{AWS::AccountId}}:layer:delta-deneb-{stage}-site-deployment-awscli"
    policies = [
        policy(template, "CloudFormationExecutionBoundary"),
        template["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"],
    ]
    for statements in policies:
        for action, resource in [
            ("lambda:PublishLayerVersion", layer),
            ("lambda:GetLayerVersion", f"{layer}:*"),
            ("lambda:DeleteLayerVersion", f"{layer}:*"),
        ]:
            grants = [item for item in statements_with_action(statements, action) if item["Effect"] == "Allow"]
            allowed = [value["Fn::Sub"] for grant in grants for value in grant["Resource"]]
            assert resource in allowed
            unrelated = [
                layer.replace(f"delta-deneb-{stage}", "another-project"),
                layer.replace(f"delta-deneb-{stage}", f"delta-deneb-{'prod' if stage == 'dev' else 'dev'}"),
                f"{layer}-other",
            ]
            for candidate in unrelated:
                if action != "lambda:PublishLayerVersion":
                    candidate += ":1"
                assert not any(fnmatch.fnmatchcase(candidate, pattern) for pattern in allowed)
    runtime = policy(template, "ApplicationRuntimeBoundary")
    assert not statements_with_action(runtime, "lambda:PublishLayerVersion")


def test_cloudfront_distribution_mutations_require_project_and_stage_tags(bootstrap):
    statements = policy(bootstrap, "CloudFormationExecutionBoundary")
    for action in ["cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution"]:
        grant, = statements_with_action(statements, action)
        assert grant["Condition"]["StringEquals"] == {"aws:ResourceTag/Project": "delta-deneb", "aws:ResourceTag/Environment": "dev"}
    removal = statements_with_action(statements, "cloudfront:UntagResource")
    assert any(item["Effect"] == "Deny" and item["Condition"]["ForAnyValue:StringEquals"]["aws:TagKeys"] == ["Project", "Environment"] for item in removal)
    for grant in statements_with_action(statements, "cloudfront:TagResource"):
        conditions = grant["Condition"]
        existing_tags = conditions.get("StringEqualsIfExists", {}) | conditions.get("StringEquals", {})
        assert existing_tags["aws:ResourceTag/Project"] == "delta-deneb"
        assert existing_tags["aws:ResourceTag/Environment"] == "dev"


def test_production_uses_different_resources_and_unreviewed_bootstrap_versions_fail():
    base = yaml.safe_load(BASE_TEMPLATE.read_text())
    prod = build_template(base, stage="prod")
    assert prod["Parameters"]["Qualifier"]["Default"] == "denebprd1"
    assert "delta-deneb-dev-*" not in json.dumps(prod)
    for resource in prod["Resources"].values():
        if resource["Type"] == "AWS::IAM::ManagedPolicy":
            assert len(json.dumps(resolved_policy(resource["Properties"]["PolicyDocument"], "prod"), separators=(",", ":"))) <= 6144
    assert base["Parameters"]["Qualifier"]["Default"] == "hnb659fds"
    base["Resources"]["CdkBootstrapVersion"]["Properties"]["Value"] = "999"
    with pytest.raises(ValueError, match="review"):
        build_template(base)
    with pytest.raises(ValueError):
        build_template(base, deployer_user_name="unexpected/path")


def test_execution_boundary_compaction_preserves_every_permission(bootstrap):
    def grants(statements):
        result = {}
        for item in statements:
            scope = {key: value for key, value in item.items() if key not in {"Sid", "Action"}}
            key = json.dumps(scope, sort_keys=True)
            result.setdefault(key, set()).update(item["Action"])
        return result

    inline = bootstrap["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert grants(inline) == grants(policy(bootstrap, "CloudFormationExecutionBoundary"))


def test_step2_runtime_and_provisioning_scopes_are_project_specific(bootstrap):
    runtime = policy(bootstrap, "ApplicationRuntimeBoundary")
    query, = statements_with_action(runtime, "dynamodb:Query")
    assert "delta-deneb-dev-telemetry/index/by-machine-received-at" in json.dumps(query["Resource"])
    assert query["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["delta_machine_alpha"]}}
    write, = statements_with_action(runtime, "dynamodb:PutItem")
    assert "delta-deneb-dev-telemetry" in json.dumps(write["Resource"])
    assert not statements_with_action(runtime, "dynamodb:DeleteItem")
    execution = policy(bootstrap, "CloudFormationExecutionBoundary")
    for action, expected in [("iot:CreateTopicRule", "delta_deneb_dev_telemetry"),
                             ("dynamodb:CreateTable", "delta-deneb-dev-telemetry"),
                             ("secretsmanager:CreateSecret", "delta-deneb/dev/viewer-token-??????")]:
        grant, = statements_with_action(execution, action)
        assert expected in json.dumps(grant["Resource"]) and grant["Resource"] != "*"
    passing = statements_with_action(execution, "iam:PassRole")
    iot, = [item for item in passing if item["Condition"]["StringEquals"]["iam:PassedToService"] == "iot.amazonaws.com"]
    assert "role/delta-deneb-dev-telemetry-ingest" in json.dumps(iot["Resource"])
