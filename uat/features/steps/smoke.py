from behave import given, then, when

from features.environment import get_machine, install_deb


@given("a `{release}` `{machine_type}` machine")
def given_machine(context, release, machine_type):
    assert release == "trusty"
    assert machine_type == "vagrant"
    context.machine = get_machine(context)


@given("a `{release}` `{machine_type}` machine with ubuntu-pro-client installed")
def given_machine_with_deb(context, release, machine_type):
    given_machine(context, release, machine_type)
    install_deb(context)


@when("I run `{command}`")
def when_run_command(context, command):
    context.process = context.machine.execute(command)


@then("the command succeeds")
def then_command_succeeds(context):
    assert context.process.return_code == 0, context.process.stderr


@then("stdout contains `{expected}`")
def then_stdout_contains(context, expected):
    assert expected in context.process.stdout
