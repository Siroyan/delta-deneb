"""CDK entry point. Synthesizing this app requires no AWS credentials."""

import os

import aws_cdk as cdk

from infra.foundation_stack import FoundationStack


app = cdk.App(outdir=os.environ.get("CDK_OUTDIR") or "cdk.out")
stage = app.node.try_get_context("stage") or "dev"
if stage not in {"dev", "prod"}:
    raise ValueError("stage must be 'dev' or 'prod'")

stack = FoundationStack(
    app,
    f"DeltaDeneb{stage.title()}",
    stage=stage,
    stack_name=f"delta-deneb-{stage}",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region="ap-northeast-1",
    ),
    description="delta-deneb static dashboard and health API foundation (Step 1)",
)
cdk.Tags.of(stack).add("Project", "delta-deneb")
cdk.Tags.of(stack).add("Environment", stage)
app.synth()
