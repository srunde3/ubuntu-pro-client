"""What a job established for its unit, decided once, when it ends.

A test framework's adapter observes the job's files and reports each
instance it finds. The rules here decide the unit's result from those
observations, the same way for every framework.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from pro_e2e.jobs.messages import (
    Failure,
    Instance,
    InstanceResult,
    InstanceStatus,
    JobResult,
    Outcome,
)


@dataclass(frozen=True)
class ObservedInstance:
    """One instance as a job's report shows it, whether or not it belongs
    to the job's unit. ``reason`` says why it was skipped, or how the test
    machinery broke."""

    id: str
    name: str
    status: InstanceStatus
    reason: str = ""
    failures: tuple[Failure, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str = ""
    instances: tuple[InstanceResult, ...] = ()
    failures: tuple[Failure, ...] = ()


def _broken(observed: Sequence[ObservedInstance]) -> ObservedInstance | None:
    for instance in observed:
        if instance.status in (InstanceStatus.FAILED, InstanceStatus.ERROR):
            return instance
    return None


def decide(
    expected: Sequence[Instance],
    observed: Sequence[ObservedInstance] | None,
    returncode: int | None,
) -> Decision:
    """The unit's result. The first rule that matches decides.

    Only the unit's own instances count. An instance the report shows that
    is not the unit's, such as a row of another combo, is used only to
    explain a non-zero exit.
    """
    if observed is None:
        return Decision(Outcome.ERROR, "the job left no readable report")
    names = {instance.id: instance.name for instance in expected}
    own = [instance for instance in observed if instance.id in names]
    if not own:
        return Decision(
            Outcome.ERROR, "the report has none of the unit's instances"
        )
    changed = _changed(names, own)
    if changed:
        return Decision(
            Outcome.ERROR,
            "the feature changed since the job was queued: " + changed,
        )

    instances = tuple(
        InstanceResult(
            id=instance.id,
            name=instance.name,
            status=instance.status,
            reason=instance.reason,
        )
        for instance in own
    )
    failures = tuple(f for instance in own for f in instance.failures)
    statuses = {instance.status for instance in own}

    def decision(outcome: Outcome, reason: str = "") -> Decision:
        return Decision(outcome, reason, instances, failures)

    if InstanceStatus.FAILED in statuses:
        return decision(Outcome.FAILED)
    if InstanceStatus.ERROR in statuses:
        broken = _broken(own)
        assert broken is not None
        return decision(
            Outcome.ERROR, "{}: {}".format(broken.name, broken.reason)
        )
    if returncode != 0:
        other = _broken([i for i in observed if i.id not in names])
        why = (
            ""
            if other is None
            else ", at {}: {} {}".format(
                other.name, other.status.value, other.reason
            ).rstrip()
        )
        return decision(
            Outcome.ERROR, "the command exited {}{}".format(returncode, why)
        )
    if statuses == {InstanceStatus.SKIPPED}:
        return decision(Outcome.SKIPPED)
    if InstanceStatus.PASSED in statuses:
        return decision(Outcome.PASSED)
    return decision(Outcome.ERROR, "no instance passed, failed or skipped")


def _changed(names: dict[str, str], own: Sequence[ObservedInstance]) -> str:
    """How the report's instances differ from those queued, or ''."""
    seen = Counter(instance.id for instance in own)
    problems = [
        "{} is missing".format(names[id]) for id in names if id not in seen
    ]
    problems += [
        "{} appears {} times".format(names[id], count)
        for id, count in seen.items()
        if count > 1
    ]
    problems += [
        "{} is now named {}".format(names[instance.id], instance.name)
        for instance in own
        if instance.name != names[instance.id]
    ]
    return "; ".join(problems)


def finished_result(
    at: str,
    returncode: int | None,
    expected: Sequence[Instance],
    observed: Sequence[ObservedInstance] | None,
) -> JobResult:
    """The result of a job whose command ran to its end."""
    decided = decide(expected, observed, returncode)
    return JobResult(
        finished_at=at,
        returncode=returncode,
        outcome=decided.outcome,
        reason=decided.reason,
        instances=list(decided.instances),
        failures=list(decided.failures),
    )


def start_failure(at: str, reason: str) -> JobResult:
    """The result of a job whose command never ran."""
    return JobResult(finished_at=at, outcome=Outcome.ERROR, reason=reason)


def killed_result(
    at: str, returncode: int | None = None, reason: str = ""
) -> JobResult:
    """The end of a killed job: it has no outcome."""
    return JobResult(
        finished_at=at, returncode=returncode, killed=True, reason=reason
    )


def unreadable_result(
    at: str, returncode: int | None, error: Exception
) -> JobResult:
    """The result when reading the job's files failed. Built from plain
    values, so it can always be written."""
    return JobResult(
        finished_at=at,
        returncode=returncode,
        outcome=Outcome.ERROR,
        reason="could not read the job's result: {!r}".format(error),
    )
