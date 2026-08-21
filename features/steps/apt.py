from behave import then

from features.steps.assertions import _run_as_root

APT_AUTH_PARTS_KEY = "Dir::Etc::netrcparts/"
APT_AUTH_FILE_KEY = "Dir::Etc::netrc/"


def apt_auth_file(context):
    """Resolve the SUT's apt auth file the same way uaclient.apt does.

    Trusty's apt predates auth.conf.d, so the path differs per release.
    """
    cached = getattr(context, "apt_auth_file", None)
    if cached:
        return cached
    out = _run_as_root(
        context, ["apt-config", "shell", "key", APT_AUTH_PARTS_KEY]
    ).strip()
    if out:
        path = out.split("'")[1] + "90ubuntu-advantage"
    else:
        out = _run_as_root(
            context, ["apt-config", "shell", "key", APT_AUTH_FILE_KEY]
        ).strip()
        path = out.split("'")[1].rstrip("/")
    context.apt_auth_file = path
    return path


def _read_apt_auth_file(context):
    path = apt_auth_file(context)
    result = context.machine.execute(["cat", path], use_sudo=True)
    if result.return_code:
        return None
    return result.stdout


@then("the apt auth file exists")
def then_apt_auth_file_exists(context):
    path = apt_auth_file(context)
    _run_as_root(context, ["test", "-f", path])


@then("the apt auth file does not exist")
def then_apt_auth_file_does_not_exist(context):
    path = apt_auth_file(context)
    _run_as_root(context, ["test", "!", "-e", path])


@then("the apt auth file contains `{expected}`")
def then_apt_auth_file_contains(context, expected):
    contents = _read_apt_auth_file(context)
    path = apt_auth_file(context)
    assert contents is not None, "{} does not exist".format(path)
    # The file holds live credentials, so never echo it into the test log.
    assert expected in contents, "Expected {!r} in {}".format(expected, path)


@then("the apt auth file does not contain `{expected}`")
def then_apt_auth_file_does_not_contain(context, expected):
    contents = _read_apt_auth_file(context)
    if contents is None:
        return
    assert expected not in contents, "Did not expect {!r} in {}".format(
        expected, apt_auth_file(context)
    )
