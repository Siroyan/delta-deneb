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
        "deployment_layer_name": f"delta-deneb-{stage}-site-deployment-awscli",
        "telemetry_table": f"delta-deneb-{stage}-telemetry",
        "telemetry_index": "by-machine-received-at",
        "iot_rule": f"delta_deneb_{stage}_telemetry",
        "mqtt_topic": "v0/delta_machine_alpha/telemetry/racing_data",
        "machine_id": "delta_machine_alpha",
        "viewer_secret": f"delta-deneb/{stage}/viewer-token",
    }
