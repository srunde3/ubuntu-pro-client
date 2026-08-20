import json

from behave import then


def _run_as_root(context, command):
    result = context.machine.execute(command, use_sudo=True)
    if result.return_code:
        raise AssertionError(
            "Command failed with return code {}\nstdout:\n{}\nstderr:\n{}".format(
                result.return_code, result.stdout, result.stderr
            )
        )
    return result.stdout


@then("the file `{path}` exists")
def then_file_exists(context, path):
    _run_as_root(context, ["test", "-f", path])


@then("the file `{path}` contains `{expected}`")
def then_file_contains(context, path, expected):
    contents = _run_as_root(context, ["cat", path])
    assert expected in contents, "Expected {!r} in {}\ncontents:\n{}".format(
        expected, path, contents
    )


@then("service `{service}` is enabled")
def then_service_is_enabled(context, service):
    raw_status = _run_as_root(context, ["ua", "status", "--format", "json"])
    status = json.loads(raw_status)
    service_status = next(
        (entry for entry in status["services"] if entry["name"] == service),
        None,
    )
    assert (
        service_status and service_status["status"] == "enabled"
    ), "Expected {} to be enabled\nstatus:\n{}".format(service, raw_status)
