"""Names shared by the application and its dedicated bootstrap template."""


def deployment_settings(stage):
    if stage not in {"dev", "prod"}:
        raise ValueError("stage must be 'dev' or 'prod'")
    return {
        "qualifier": {"dev": "denebdev1", "prod": "denebprd1"}[stage],
        "stack_name": f"delta-deneb-{stage}",
        "toolkit_name": f"DeltaDeneb{stage.title()}Toolkit",
        "runtime_boundary": f"delta-deneb-runtime-boundary-{stage}",
        "execution_boundary": f"delta-deneb-execution-boundary-{stage}",
    }
