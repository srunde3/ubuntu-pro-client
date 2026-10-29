"""What a job runs, and how its state is read."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from pro_e2e.jobs.domain.pools import POOL_OF, Pool
from pro_e2e.jobs.messages import (
    Instance,
    Interruption,
    JobRequest,
    JobResult,
    JobState,
)


@dataclass(frozen=True)
class Unit:
    """One scenario on one release and one machine type."""

    feature: str
    scenario: str
    release: str
    machine_type: str


@dataclass(frozen=True)
class Invocation:
    """How a test framework runs one unit: the command a job holds, and the
    unit's own instances, which its result will count."""

    command: list[str] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)


class InstallFrom(str, Enum):
    """Subset of features/util.py's InstallationSource exposed here.

    Excludes CUSTOM and PREBUILT, which require extra config (custom_ppa,
    debs_path) that a job is not asked for.
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
            f"pro-e2e config set max_jobs.{pool} N to allow "
            f"{machine_type}."
        )
    return None


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
    *, result: JobResult | None, launched: bool, lock_held: bool
) -> JobState:
    """A job's state from its result, its launch record and its lock.

    The job process holds the job lock for its whole life and writes the
    result before it lets go, so a result decides: a killed job is
    interrupted, any other has finished. Without one, a job never launched
    is queued; a launched one is running while its lock is held, and its
    process died if the lock is free. The launch takes the lock before it
    writes the launch record, so a launch in progress never reads as dead.
    """
    if result is not None:
        return JobState.INTERRUPTED if result.killed else JobState.FINISHED
    if not launched:
        return JobState.QUEUED
    if lock_held:
        return JobState.RUNNING
    return JobState.INTERRUPTED


def interruption(result: JobResult | None) -> Interruption:
    """Why an interrupted job ended without a result."""
    if result is not None and result.killed:
        return Interruption.KILLED
    return Interruption.DIED


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
    if scenario is not None and scenario != record.scenario:
        return False
    if release is not None and release != record.release:
        return False
    if machine_type is not None and machine_type != record.machine_type:
        return False
    return True
