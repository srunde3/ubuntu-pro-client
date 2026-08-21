import json
from dataclasses import dataclass
from typing import List, Optional

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


# Queried through python-apt on the SUT
APT_QUERY = """
import json
import sys

import apt

version = apt.Cache()[sys.argv[1]].installed
print(json.dumps({
    "installed": version.version if version else None,
    "uris": list(version.uris) if version else [],
    "origins": [
        {"site": o.site, "archive": o.archive, "origin": o.origin}
        for o in (version.origins if version else [])
    ],
}))
"""


@dataclass
class Origin:
    site: str
    archive: str
    origin: str


@dataclass
class PackageQuery:
    """Installed state of one package, as reported by python-apt on the SUT."""

    installed: Optional[str]
    uris: List[str]
    origins: List[Origin]

    @classmethod
    def from_json(cls, raw: str) -> "PackageQuery":
        data = json.loads(raw)
        return cls(
            installed=data["installed"],
            uris=data["uris"],
            origins=[Origin(**origin) for origin in data["origins"]],
        )


def _query_package(context, package):
    raw = _run_as_root(context, ["python3", "-c", APT_QUERY, package])
    return PackageQuery.from_json(raw)


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
    _assert_service_status(context, service, "enabled")


@then("service `{service}` is disabled")
def then_service_is_disabled(context, service):
    _assert_service_status(context, service, "disabled")


def _assert_service_status(context, service, expected):
    raw_status = _run_as_root(context, ["ua", "status", "--format", "json"])
    status = json.loads(raw_status)
    service_status = next(
        (entry for entry in status["services"] if entry["name"] == service),
        None,
    )
    assert (
        service_status and service_status["status"] == expected
    ), "Expected {} to be {}\nstatus:\n{}".format(service, expected, raw_status)


@then("apt policy contains origin `{origin}`")
def then_apt_policy_contains_origin(context, origin):
    policy = _run_as_root(context, ["apt-cache", "policy"])
    assert (
        "o={}".format(origin) in policy
    ), "Expected origin {!r} in apt-cache policy\n{}".format(origin, policy)


@then("the installed version of `{package}` comes from `{source}`")
def then_installed_version_comes_from(context, package, source):
    pkg = _query_package(context, package)
    assert pkg.installed, "{} is not installed".format(package)
    assert any(
        source in uri for uri in pkg.uris
    ), "Installed {} {} does not come from {!r}\n{}".format(
        package, pkg.installed, source, pkg
    )
