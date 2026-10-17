"""Generate a dedicated, restricted CDK bootstrap template without AWS access.

The base template is read from the pinned AWS CDK CLI installed by npm ci.
Only the generated JSON is deployed, by the user's administrator profile.
"""

import argparse
import copy
import json
from pathlib import Path
import re

import yaml

from infra.deployment_settings import deployment_settings


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = ROOT / "node_modules/aws-cdk/lib/api/bootstrap/bootstrap-template.yaml"
EXPECTED_CDK_CLI_VERSION = "2.1145.0"


def sub(value):
    return {"Fn::Sub": value}


def statement(sid, actions, resources, *, effect="Allow", conditions=None):
    value = {"Sid": sid, "Effect": effect, "Action": actions, "Resource": resources}
    if conditions:
        value["Condition"] = conditions
    return value


def document(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def build_template(base, *, stage="dev", deployer_user_name="delta-deneb-deployer"):
    """Return an account-independent template; never read profiles or credentials."""
    if not re.fullmatch(r"[A-Za-z0-9_+=,.@-]{1,64}", deployer_user_name):
        raise ValueError("deployer user name must be an IAM user name without a path")
    settings = deployment_settings(stage)
    template = copy.deepcopy(base)
    resources = template["Resources"]
    if resources["CdkBootstrapVersion"]["Properties"]["Value"] != "32":
        raise ValueError("Unexpected CDK bootstrap version; review the pinned CLI template before updating")
    resources.pop("CdkBoostrapPermissionsBoundaryPolicy")
    template["Description"] = f"delta-deneb {stage}: dedicated CDK bootstrap and permission guardrails"
    # This variant deliberately does not expose trust or policy override inputs.
    template["Parameters"] = {
        "Qualifier": {"Type": "String", "Default": settings["qualifier"], "AllowedValues": [settings["qualifier"]]},
        "BootstrapVariant": {"Type": "String", "Default": "delta-deneb restricted bootstrap v1", "AllowedValues": ["delta-deneb restricted bootstrap v1"]},
    }
    template["Metadata"] = {"BootstrapVariant": {"Ref": "BootstrapVariant"}, "Source": f"AWS CDK CLI {EXPECTED_CDK_CLI_VERSION}, bootstrap version 32"}
    template.pop("Conditions")
    resources.pop("FileAssetsBucketEncryptionKey")
    resources.pop("FileAssetsBucketEncryptionKeyAlias")
    template["Outputs"].pop("FileAssetKeyArn")
    bucket = resources["StagingBucket"]["Properties"]
    bucket["BucketName"] = sub("cdk-${Qualifier}-assets-${AWS::AccountId}-${AWS::Region}")
    bucket["PublicAccessBlockConfiguration"] = {key: True for key in ["BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"]}
    bucket.pop("AccessControl")
    bucket["OwnershipControls"] = {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    resources["ContainerAssetsRepository"]["Properties"]["RepositoryName"] = sub("cdk-${Qualifier}-container-assets-${AWS::AccountId}-${AWS::Region}")

    # Step 1 uses S3-managed encryption; no KMS key permissions are necessary.
    resources["StagingBucket"]["Properties"]["BucketEncryption"] = {
        "ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    }
    publishing = resources["FilePublishingRoleDefaultPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    resources["FilePublishingRoleDefaultPolicy"]["Properties"]["PolicyDocument"]["Statement"] = [
        item for item in publishing if not any(action.startswith("kms:") for action in item["Action"])
    ]

    prefix = settings["stack_name"]
    region_account = "${AWS::Region}:${AWS::AccountId}"
    role_arn = sub(f"arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/{prefix}-*")
    runtime_boundary = sub(f"arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:policy/{settings['runtime_boundary']}")
    site = [sub(f"arn:${{AWS::Partition}}:s3:::{prefix}-*"), sub(f"arn:${{AWS::Partition}}:s3:::{prefix}-*/*")]
    assets = [sub("${StagingBucket.Arn}"), sub("${StagingBucket.Arn}/*")]
    logs = [
        sub(f"arn:${{AWS::Partition}}:logs:{region_account}:log-group:{prefix}-*"),
        sub(f"arn:${{AWS::Partition}}:logs:{region_account}:log-group:/aws/lambda/{prefix}-*"),
    ]
    distribution = sub("arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/*")
    version_arn = sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/cdk-bootstrap/${Qualifier}/version")
    deployment_layer_arn = f"arn:${{AWS::Partition}}:lambda:{region_account}:layer:{settings['deployment_layer_name']}"
    resource_tags = {"aws:ResourceTag/Project": "delta-deneb", "aws:ResourceTag/Environment": stage}
    request_tags = {"aws:RequestTag/Project": "delta-deneb", "aws:RequestTag/Environment": stage}
    log_permissions = statement("ApplicationLogs", ["logs:*"], logs)
    asset_reads = statement("ReadOwnAssets", ["s3:GetObject*", "s3:GetBucket*", "s3:List*"], assets)

    runtime_statements = [
        log_permissions,
        statement("ApplicationObjects", ["s3:Get*", "s3:List*", "s3:PutObject*", "s3:DeleteObject*", "s3:AbortMultipartUpload"], site),
        asset_reads,
        statement("InvalidateProjectDistribution", ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"], [distribution], conditions={"StringEquals": resource_tags}),
        statement("NoIdentityOrRoleEscalation", ["iam:*", "sts:*", "organizations:*", "account:*"], "*", effect="Deny"),
    ]
    resources["ApplicationRuntimeBoundary"] = {
        "Type": "AWS::IAM::ManagedPolicy",
        "Properties": {"ManagedPolicyName": settings["runtime_boundary"], "PolicyDocument": document(runtime_statements)},
    }
    execution_statements = [
        statement("ApplicationBuckets", ["s3:*"], site),
        asset_reads,
        statement("ResolveBootstrapVersion", ["ssm:GetParameter", "ssm:GetParameters"], [version_arn]),
        log_permissions,
        statement("DiscoverLogGroups", ["logs:DescribeLogGroups"], "*"),
        statement("ApplicationFunctions", ["lambda:*"], [sub(f"arn:${{AWS::Partition}}:lambda:{region_account}:function:{prefix}-*")]),
        statement("DeploymentLayerVersions", ["lambda:PublishLayerVersion", "lambda:GetLayerVersion", "lambda:DeleteLayerVersion"], [sub(deployment_layer_arn), sub(f"{deployment_layer_arn}:*")]),
        statement("CreateTaggedDistribution", ["cloudfront:CreateDistribution"], "*", conditions={"StringEquals": request_tags}),
        statement("ManageTaggedDistribution", ["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution", "cloudfront:ListTagsForResource"], [distribution], conditions={"StringEquals": resource_tags}),
        statement("TagNewDistribution", ["cloudfront:TagResource"], [distribution], conditions={"StringEquals": request_tags, "StringEqualsIfExists": resource_tags}),
        statement("TagProjectDistribution", ["cloudfront:TagResource", "cloudfront:UntagResource"], [distribution], conditions={"StringEquals": resource_tags, "StringEqualsIfExists": request_tags}),
        statement("PreserveIsolationTags", ["cloudfront:UntagResource"], [distribution], effect="Deny", conditions={"ForAnyValue:StringEquals": {"aws:TagKeys": ["Project", "Environment"]}}),
        # These CloudFront APIs do not support resource-level IAM restrictions.
        statement("CloudFrontSharedConfiguration", [
            "cloudfront:CreateCachePolicy", "cloudfront:GetCachePolicy", "cloudfront:GetCachePolicyConfig", "cloudfront:UpdateCachePolicy", "cloudfront:DeleteCachePolicy",
            "cloudfront:CreateOriginRequestPolicy", "cloudfront:GetOriginRequestPolicy", "cloudfront:GetOriginRequestPolicyConfig", "cloudfront:UpdateOriginRequestPolicy", "cloudfront:DeleteOriginRequestPolicy",
            "cloudfront:CreateOriginAccessControl", "cloudfront:GetOriginAccessControl", "cloudfront:GetOriginAccessControlConfig", "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl",
            "cloudfront:GetResponseHeadersPolicy", "cloudfront:GetResponseHeadersPolicyConfig",
        ], "*"),
        statement("CreateBoundedApplicationRoles", ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], [role_arn], conditions={"StringEquals": {"iam:PermissionsBoundary": runtime_boundary}}),
        statement("ManageApplicationRoles", [
            "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription", "iam:UpdateAssumeRolePolicy",
            "iam:TagRole", "iam:UntagRole", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
        ], [role_arn]),
        statement("BasicLambdaExecutionPolicy", ["iam:AttachRolePolicy", "iam:DetachRolePolicy"], [role_arn], conditions={"ArnEquals": {"iam:PolicyARN": sub("arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")}}),
        statement("PassApplicationRolesToLambdaOnly", ["iam:PassRole"], [role_arn], conditions={"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}),
        statement("RequireRuntimeBoundary", ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], "*", effect="Deny", conditions={"StringNotEquals": {"iam:PermissionsBoundary": runtime_boundary}}),
        statement("PreventBoundaryRemoval", ["iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary"], "*", effect="Deny"),
        statement("PreventPolicyAndUserAdministration", ["iam:CreatePolicy*", "iam:DeletePolicy*", "iam:SetDefaultPolicyVersion", "iam:CreateUser", "iam:CreateAccessKey", "iam:PutUserPolicy", "iam:AttachUserPolicy", "sts:AssumeRole"], "*", effect="Deny"),
    ]
    resources["CloudFormationExecutionBoundary"] = {
        "Type": "AWS::IAM::ManagedPolicy",
        "Properties": {"ManagedPolicyName": settings["execution_boundary"], "PolicyDocument": document(execution_statements)},
    }
    execution = resources["CloudFormationExecutionRole"]["Properties"]
    execution.pop("ManagedPolicyArns")
    execution["Policies"] = [{"PolicyName": "ApplicationProvisioning", "PolicyDocument": document(execution_statements)}]
    execution["PermissionsBoundary"] = {"Ref": "CloudFormationExecutionBoundary"}

    # Trust this user through the account principal, so identity permissions are
    # still required. No account-wide or cross-account trust remains.
    user_arn = sub(f"arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:user/{deployer_user_name}")
    for key in ["DeploymentActionRole", "FilePublishingRole", "ImagePublishingRole", "LookupRole"]:
        resources[key]["Properties"]["AssumeRolePolicyDocument"] = {
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Principal": {"AWS": sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:root")},
                "Condition": {"ArnEquals": {"aws:PrincipalArn": user_arn}, "Null": {"sts:ExternalId": "true"}},
            }],
        }

    app_stack_arn = sub(f"arn:${{AWS::Partition}}:cloudformation:{region_account}:stack/{prefix}/*")
    toolkit_stack_arn = sub(f"arn:${{AWS::Partition}}:cloudformation:{region_account}:stack/{settings['toolkit_name']}/*")
    reads = statement("ReadProjectStacks", [
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents", "cloudformation:DescribeStackResources", "cloudformation:ListStackResources",
        "cloudformation:GetTemplate", "cloudformation:GetStackPolicy", "cloudformation:DescribeChangeSet", "cloudformation:ListChangeSets",
    ], [app_stack_arn, toolkit_stack_arn])
    version_read = statement("ReadBootstrapVersion", ["ssm:GetParameter", "ssm:GetParameters"], [version_arn])
    deploy = resources["DeploymentActionRole"]["Properties"]
    deploy.pop("ManagedPolicyArns")
    deploy["Policies"] = [{"PolicyName": "ProjectDeployment", "PolicyDocument": document([
        statement("DeployProjectStack", ["cloudformation:CreateChangeSet", "cloudformation:DeleteChangeSet", "cloudformation:ExecuteChangeSet", "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:RollbackStack", "cloudformation:ContinueUpdateRollback", "cloudformation:DeleteStack", "cloudformation:UpdateTerminationProtection", "cloudformation:DescribeStackResource", "cloudformation:DescribeEvents", "cloudformation:GetHookResult"], [app_stack_arn]),
        reads, version_read,
        statement("ReadTemplateSummary", ["cloudformation:GetTemplateSummary"], "*"),
        statement("PassOwnExecutionRole", ["iam:PassRole"], [sub("${CloudFormationExecutionRole.Arn}")], conditions={"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}),
        asset_reads,
    ])}]
    lookup = resources["LookupRole"]["Properties"]
    lookup.pop("ManagedPolicyArns")
    lookup["Policies"] = [{"PolicyName": "ProjectLookup", "PolicyDocument": document([reads, version_read])}]
    resources["DeployerUserPolicy"] = {
        "Type": "AWS::IAM::Policy", "Properties": {
            "PolicyName": f"delta-deneb-{stage}-deployment", "Users": [deployer_user_name],
            "PolicyDocument": document([
                statement("AssumeProjectRoles", ["sts:AssumeRole", "sts:TagSession"], [sub("${DeploymentActionRole.Arn}"), sub("${FilePublishingRole.Arn}"), sub("${LookupRole.Arn}")]),
                reads, version_read,
            ]),
        },
    }
    template["Outputs"]["RuntimeBoundaryArn"] = {"Value": {"Ref": "ApplicationRuntimeBoundary"}}
    template["Outputs"]["CloudFormationExecutionRoleArn"] = {"Value": sub("${CloudFormationExecutionRole.Arn}")}
    return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["dev", "prod"], default="dev")
    parser.add_argument("--deployer-user-name", default="delta-deneb-deployer")
    args = parser.parse_args()
    cli_version = json.loads((ROOT / "node_modules/aws-cdk/package.json").read_text())["version"]
    if cli_version != EXPECTED_CDK_CLI_VERSION:
        raise ValueError("CDK CLI changed; review bootstrap permissions before generating a new template")
    with BASE_TEMPLATE.open() as source:
        base = yaml.safe_load(source)
    template = build_template(base, stage=args.stage, deployer_user_name=args.deployer_user_name)
    output = ROOT / "bootstrap.out" / f"{args.stage}.template.json"
    contents = json.dumps(template, indent=2) + "\n"
    if len(contents.encode()) > 51200:
        contents = json.dumps(template, separators=(",", ":")) + "\n"
    if len(contents.encode()) > 51200:
        raise ValueError("Bootstrap template exceeds the CloudFormation inline template limit")
    output.parent.mkdir(exist_ok=True)
    output.write_text(contents)
    print(f"Generated {output.relative_to(ROOT)}; AWS was not contacted.")
    print(f"Administrator stack name: {deployment_settings(args.stage)['toolkit_name']}")


if __name__ == "__main__":
    main()
