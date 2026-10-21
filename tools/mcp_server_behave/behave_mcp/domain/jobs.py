"""How a behave job is built, validated and classified."""

import re
from enum import Enum
from pathlib import Path
from typing import Collection, Iterable, Mapping

from behave_mcp.domain.pools import POOL_OF, Pool
from behave_mcp.messages import Interruption, JobExit, JobRequest, JobState

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
    machine_type: str, max_jobs: Mapping[Pool, int]
) -> str | None:
    if machine_type not in POOL_OF:
        allowed = ",".join(sorted(POOL_OF))
        return (
            f"Unsupported machine_type: {machine_type}. "
            f"Allowed values: {allowed}"
        )
    pool = POOL_OF[machine_type]
    if not max_jobs.get(pool):
        return (
            f"{pool} machine types are disabled for this checkout. Run "
            f"behave-tests config set max_jobs.{pool} N to allow "
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
    scenario: str,
    release: str,
    machine_type: str,
) -> UnitMatch:
    """Whether exactly one scenario runs this name on this combo.

    Two scenarios may share a name when they cover different combos; only
    a combo both cover makes the unit ambiguous, because behave would run
    them both.
    """
    named = [combos for name, combos in scenarios if name == scenario]
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


def job_state(
    *, exit: JobExit | None, launched: bool, lock_held: bool
) -> JobState:
    """A job's state from its exit, its launch record and its lock.

    The job process holds the job lock for its whole life and writes the
    exit before it lets go, so an exit decides: a killed job is
    interrupted, any other has finished. Without one, a job never launched
    is queued; a launched one is running while its lock is held, and its
    process died if the lock is free. The launch takes the lock before it
    writes the launch record, so a launch in progress never reads as dead.
    """
    if exit is not None:
        return JobState.INTERRUPTED if exit.killed else JobState.FINISHED
    if not launched:
        return JobState.QUEUED
    if lock_held:
        return JobState.RUNNING
    return JobState.INTERRUPTED


def interruption(exit: JobExit | None) -> Interruption:
    """Why an interrupted job ended without a result."""
    if exit is not None and exit.killed:
        return Interruption.KILLED
    return Interruption.DIED


def exact_name_pattern(scenario: str) -> str:
    """A behave ``--name`` regex matching this scenario and nothing else.

    behave names each Scenario Outline row ``<name> -- @<row> <examples>``,
    so the row suffix is allowed; any other longer name is not.
    """
    return "^{}(?: -- @.*)?$".format(re.escape(scenario))


def build_command(
    feature: str,
    scenario: str,
    release: str,
    machine_type: str,
    json_report_path: Path,
) -> list[str]:
    """The behave run for exactly one unit."""
    command = ["tox", "-e", "behave", "--", feature]
    command.extend(["--name", exact_name_pattern(scenario)])
    command.extend(["-D", f"releases={release}"])
    command.extend(["-D", f"machine_types={machine_type}"])
    command.extend(["-f", "json", "-o", str(json_report_path)])
    command.extend(["-f", "plain"])
    return command


def job_matches_result_filters(
    record: JobRequest,
    *,
    job_id: str,
    job_ids: set[str] | None,
    feature: str | None,
    scenario: str | None,
    release: str | None,
    machine_type: str | None,
) -> bool:
    """Return whether a job's request satisfies ``job_results``
    filters. Only ``job_ids`` inspects ``job_id`` directly; the rest read
    fields of what the job was asked to run.
    """
    if job_ids is not None and job_id not in job_ids:
        return False
    if feature is not None and record.feature != feature:
        return False
    if scenario is not None:
        if scenario.lower() not in record.scenario.lower():
            return False
    if release is not None and release != record.release:
        return False
    if machine_type is not None and machine_type != record.machine_type:
        return False
    return True
