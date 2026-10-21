"""How a behave job is built, validated and classified."""

import re
from enum import Enum
from pathlib import Path
from typing import Collection, Iterable, Mapping

from behave_mcp.domain.pools import POOL_LIMIT_ENV_VARS, POOL_OF, Pool
from behave_mcp.messages import JobRecord, RunStatus

# features/environment.py reads this exact name (UAClientBehaveConfig's
# UACLIENT_BEHAVE_ prefix + install_from) to pick where the behave
# subprocess installs ubuntu-pro-client from; it defaults to 'local' when
# unset, same as InstallFrom.LOCAL below, but we still set it explicitly so
# non-default values (e.g. 'proposed') actually reach that subprocess.
INSTALL_FROM_ENV_VAR = "UACLIENT_BEHAVE_INSTALL_FROM"


class InstallFrom(str, Enum):
    """Subset of features/util.py's InstallationSource exposed here.

    Excludes CUSTOM and PREBUILT, which require extra config (custom_ppa,
    debs_path) not exposed through this MCP interface.
    """

    LOCAL = "local"
    ARCHIVE = "archive"
    DAILY = "daily"
    STAGING = "staging"
    STABLE = "stable"
    PROPOSED = "proposed"


DEFAULT_INSTALL_FROM = (
    InstallFrom.LOCAL.value
)  # the pro client-defined default.


def validate_machine_type(
    machine_type: str, pool_limits: Mapping[Pool, int]
) -> str | None:
    if machine_type not in POOL_OF:
        allowed = ",".join(sorted(POOL_OF))
        return (
            f"Unsupported machine_type: {machine_type}. "
            f"Allowed values: {allowed}"
        )
    pool = POOL_OF[machine_type]
    if not pool_limits.get(pool):
        return (
            f"{pool} machine types are disabled on this server. Set "
            f"{POOL_LIMIT_ENV_VARS[pool]} to a positive integer to allow "
            f"{machine_type}."
        )
    return None


class UnitMatch(str, Enum):
    """How a requested unit matches the scenarios of one feature file."""

    FOUND = "found"
    NO_SCENARIO = "no_scenario"
    NOT_ON_COMBO = "not_on_combo"
    AMBIGUOUS = "ambiguous"


# One scenario as unit matching sees it: its name and the
# (release, machine_type) pairs its Examples rows cover.
ScenarioCombos = tuple[str, Collection[tuple[str, str]]]


def match_unit(
    scenarios: Iterable[ScenarioCombos],
    scenario_name: str,
    release: str,
    machine_type: str,
) -> UnitMatch:
    """Whether exactly one scenario runs this name on this combo.

    Two scenarios may share a name when they cover different combos; only
    a combo both cover makes the unit ambiguous, because behave would run
    them both.
    """
    named = [combos for name, combos in scenarios if name == scenario_name]
    if not named:
        return UnitMatch.NO_SCENARIO
    covering = sum(1 for combos in named if (release, machine_type) in combos)
    if covering == 0:
        return UnitMatch.NOT_ON_COMBO
    if covering > 1:
        return UnitMatch.AMBIGUOUS
    return UnitMatch.FOUND


def validate_install_from(install_from: str) -> str | None:
    try:
        InstallFrom(install_from)
    except ValueError:
        allowed = sorted(member.value for member in InstallFrom)
        return (
            f"Unsupported install_from: {install_from}. Allowed values: "
            f"{','.join(allowed)}"
        )
    return None


def job_state(*, exited: bool, lock_held: bool) -> RunStatus:
    """A job's state from its files and its lock.

    The wrapper holds the job lock for its whole life and writes the exit
    before it lets go, so an exit decides; without one, a held lock means
    the wrapper is alive, and a free one means it died without finishing.
    """
    if exited:
        return RunStatus.COMPLETED
    if lock_held:
        return RunStatus.RUNNING
    return RunStatus.INTERRUPTED


def exact_name_pattern(scenario_name: str) -> str:
    """A behave ``--name`` regex matching this scenario and nothing else.

    behave names each Scenario Outline row ``<name> -- @<row> <examples>``,
    so the row suffix is allowed; any other longer name is not.
    """
    return "^{}(?: -- @.*)?$".format(re.escape(scenario_name))


def build_command(
    feature_file: str,
    scenario_name: str,
    release: str,
    machine_type: str,
    json_report_path: Path,
) -> list[str]:
    """The behave run for exactly one unit."""
    command = ["tox", "-e", "behave", "--", feature_file]
    command.extend(["--name", exact_name_pattern(scenario_name)])
    command.extend(["-D", f"releases={release}"])
    command.extend(["-D", f"machine_types={machine_type}"])
    command.extend(["-f", "json", "-o", str(json_report_path)])
    command.extend(["-f", "plain"])
    return command


def job_matches_result_filters(
    record: JobRecord,
    *,
    job_id: str,
    job_ids: set[str] | None,
    feature_file: str | None,
    scenario_name: str | None,
    release: str | None,
    machine_type: str | None,
) -> bool:
    """Return whether a job's record satisfies ``get_scenario_results``
    filters. Only ``job_ids`` inspects ``job_id`` directly; the rest read
    fields already present in that job's record.
    """
    if job_ids is not None and job_id not in job_ids:
        return False
    if feature_file is not None and record.feature_file != feature_file:
        return False
    if scenario_name is not None:
        if scenario_name.lower() not in record.scenario_name.lower():
            return False
    if release is not None and release != record.release:
        return False
    if machine_type is not None and machine_type != record.machine_type:
        return False
    return True
