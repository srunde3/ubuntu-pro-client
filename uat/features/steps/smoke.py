import os
import shlex
import tempfile
import uuid

from behave import given, then, when

from features.environment import get_machine


@given("a `{release}` `{machine_type}` machine")
def given_machine(context, release, machine_type):
    assert release == "trusty"
    assert machine_type == "vagrant"
    context.machine = get_machine(context)


@given("a `{release}` `{machine_type}` machine with ubuntu-pro-client installed")
def given_machine_with_deb(context, release, machine_type):
    given_machine(context, release, machine_type)


@when("I run `{command}`")
def when_run_command(context, command):
    context.process = context.machine.execute(command)


@when("I run `{command}` as sudo")
def when_run_command_as_sudo(context, command):
    context.process = context.machine.execute(command, use_sudo=True)


@when("I attach using the configured contract token")
def when_attach_using_configured_token(context):
    token = context.uat_config.contract_token
    if not token:
        raise RuntimeError(
            "Set UACLIENT_UAT_CONTRACT_TOKEN before running the attach smoke"
        )
    context.process = context.machine.execute(
        "ua attach {} --no-auto-enable".format(shlex.quote(token)),
        use_sudo=True,
    )


@then("the command succeeds")
def then_command_succeeds(context):
    assert (
        context.process.return_code == 0
    ), "return code: {}\nstdout:\n{}\nstderr:\n{}".format(
        context.process.return_code,
        context.process.stdout,
        context.process.stderr,
    )


@then("stdout contains `{expected}`")
def then_stdout_contains(context, expected):
    assert expected in context.process.stdout


@when("I transfer a file containing `{content}` through the machine")
def when_transfer_file(context, content):
    remote_path = "/tmp/uat-round-trip-{}".format(uuid.uuid4().hex)
    source = tempfile.NamedTemporaryFile(mode="w", delete=False)
    destination = None
    try:
        source.write(content)
        source.close()
        context.machine.push_file(source.name, remote_path)
        destination = tempfile.NamedTemporaryFile(delete=False)
        destination.close()
        context.machine.pull_file(remote_path, destination.name)
        with open(destination.name) as transferred:
            context.transferred_content = transferred.read()
    finally:
        if not source.closed:
            source.close()
        os.unlink(source.name)
        if destination is not None:
            os.unlink(destination.name)
        context.machine.execute(["rm", "-f", remote_path])


@then("the transferred file contains `{expected}`")
def then_transferred_file_contains(context, expected):
    assert context.transferred_content == expected
