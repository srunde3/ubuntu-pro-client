import shlex

from behave import when


@when("I attach using the configured contract token")
def when_attach_using_configured_token(context):
    _attach(context, "--no-auto-enable")


@when("I attach using the configured contract token with auto-enable")
def when_attach_with_auto_enable(context):
    _attach(context, "")


def _attach(context, options):
    token = context.uat_config.contract_token
    if not token:
        raise RuntimeError(
            "Set UACLIENT_UAT_CONTRACT_TOKEN before running the attach UAT"
        )
    context.process = context.machine.execute(
        "ua attach {} {}".format(shlex.quote(token), options).strip(),
        use_sudo=True,
    )
