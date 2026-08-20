import shlex

from behave import when


@when("I attach using the configured contract token")
def when_attach_using_configured_token(context):
    token = context.uat_config.contract_token
    if not token:
        raise RuntimeError(
            "Set UACLIENT_UAT_CONTRACT_TOKEN before running the attach UAT"
        )
    context.process = context.machine.execute(
        "ua attach {} --no-auto-enable".format(shlex.quote(token)),
        use_sudo=True,
    )
