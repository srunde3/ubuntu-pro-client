"""What an ended job established for its unit."""

from __future__ import annotations

from dataclasses import dataclass

from pro_behave.campaign.domain.events import (
    MAX_EVENT_ERROR_CHARS,
    MAX_EVENT_FAILURES,
)
from pro_behave.campaign.domain.records import (
    Failure,
    FinishedRecord,
    InstanceResult,
    InterruptedRecord,
    Outcome,
    Unit,
)

JOB_FINISHED = "finished"


# A job that ended without a result: it was killed, or its process died.
JOB_INTERRUPTED = "interrupted"


# The states of a job that has ended.
JOB_ENDED = (JOB_FINISHED, JOB_INTERRUPTED)


# Why an attempt is interrupted: its job was killed, or its process died.
KILLED = "killed"
DIED = "died"
INTERRUPTIONS = (KILLED, DIED)


@dataclass(frozen=True)
class JobStatus:
    """A job as a campaign sees it when it looks.

    ``outcome``, ``reason``, ``instances`` and ``failures`` are the result a
    finished job decided; ``interruption`` says why an interrupted one has
    none.
    """

    state: str
    job_id: str
    started_at: str = ""
    outcome: str | None = None
    reason: str = ""
    interruption: str = ""
    instances: tuple[InstanceResult, ...] = ()
    failures: tuple[Failure, ...] = ()

    @property
    def ended(self) -> bool:
        return self.state in JOB_ENDED


def _capped(failures: tuple[Failure, ...]) -> tuple[Failure, ...]:
    """The first failing steps, enough to triage without the job's files."""
    return tuple(
        Failure(
            instance=failure.instance,
            step=failure.step,
            status=failure.status,
            error_message=failure.error_message[:MAX_EVENT_ERROR_CHARS],
        )
        for failure in failures[:MAX_EVENT_FAILURES]
    )


def classify_result(
    unit: Unit, status: JobStatus, at: str = ""
) -> FinishedRecord | InterruptedRecord:
    """The record that closes an ended job's attempt.

    A job that was killed, or whose process died, established nothing, so
    its attempt is interrupted and the unit may be queued again. A finished
    job's attempt takes the result the job decided. Never raises: a
    scheduler has nobody to raise at.
    """
    if status.state == JOB_INTERRUPTED:
        return InterruptedRecord(
            unit=unit,
            job_id=status.job_id,
            at=at,
            reason=(
                status.interruption
                if status.interruption in INTERRUPTIONS
                else DIED
            ),
        )
    try:
        outcome = Outcome(status.outcome)
    except ValueError:
        return FinishedRecord(
            unit=unit,
            job_id=status.job_id,
            outcome=Outcome.ERROR,
            at=at,
            reason=(
                "the campaign could not read the job's status: "
                "outcome {!r}".format(status.outcome)
            ),
        )
    return FinishedRecord(
        unit=unit,
        job_id=status.job_id,
        outcome=outcome,
        at=at,
        reason=status.reason,
        instances=status.instances,
        failures=_capped(status.failures),
    )
