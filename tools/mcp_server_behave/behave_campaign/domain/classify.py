"""What a finished MCP job established for its unit."""

from __future__ import annotations

from typing import Any

from behave_campaign.domain.events import failure_details
from behave_campaign.domain.records import (
    CampaignError,
    Failure,
    FinishedRecord,
    InterruptedRecord,
    Outcome,
    Unit,
    text_field,
)

MCP_FINISHED = "finished"


# A job that ended without a result: it was killed, or its process died.
MCP_INTERRUPTED = "interrupted"


# The statuses a job that has stopped running reports.
MCP_ENDED = (MCP_FINISHED, MCP_INTERRUPTED)


# Why an attempt is interrupted: its job was killed, or its process died.
KILLED = "killed"
DIED = "died"
INTERRUPTIONS = (KILLED, DIED)


def _completed_outcome(result: dict[str, Any]) -> Outcome:
    summary = result.get("summary")
    if summary is None:
        return Outcome.ERROR
    if not isinstance(summary, dict):
        raise CampaignError("MCP result 'summary' must be an object or null")

    raw_counts = summary.get("scenarios")
    if not isinstance(raw_counts, dict) or not raw_counts:
        raise CampaignError("MCP result reports no scenario counts")

    counts: dict[str, int] = {}
    for name, value in raw_counts.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise CampaignError(
                "scenario count '{}' must be an integer".format(name)
            )
        counts[name] = value

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)
    other = {
        name: count
        for name, count in counts.items()
        if count and name not in ("passed", "failed", "skipped", "total")
    }

    if not other:
        if failed:
            return Outcome.FAILED
        if passed:
            if result.get("ok") is False:
                raise CampaignError(
                    "MCP reports ok=false but every scenario passed"
                )
            return Outcome.PASSED
        if skipped and not passed:
            return Outcome.SKIPPED

    raise CampaignError(
        "cannot classify scenario counts {}; record an explicit "
        "outcome".format(counts)
    )


def finished_from_mcp(unit: Unit, result: Any) -> FinishedRecord:
    """Map one completed MCP job payload onto a finished attempt.

    Only a finished job has an outcome. Handing a job still in flight here
    means something is being recorded that nobody has an outcome for yet.
    """
    if not isinstance(result, dict):
        raise CampaignError("each MCP result must be a JSON object")

    status = result.get("status")
    if status != MCP_FINISHED:
        raise CampaignError(
            "cannot record an outcome for MCP status '{}'; only a "
            "finished job has one".format(status)
        )
    if "job_id" not in result:
        raise CampaignError("MCP result is missing 'job_id'")

    return FinishedRecord(
        unit=unit,
        job_id=text_field(result, "job_id", "MCP result"),
        outcome=_completed_outcome(result),
    )


def classify_result(
    unit: Unit, result: Any, at: str = ""
) -> FinishedRecord | InterruptedRecord:
    """Map an ended MCP job onto the record that closes its attempt.

    Never raises. A job that was killed, or whose process died,
    established nothing, so its attempt is interrupted and the unit may be
    queued again. A result that
    cannot be classified is recorded as ``error`` with the reason in
    ``reason``: a scheduler has nobody to raise at.
    """
    if isinstance(result, dict) and result.get("status") == MCP_INTERRUPTED:
        reason = result.get("reason")
        return InterruptedRecord(
            unit=unit,
            job_id=str(result.get("job_id", "")),
            at=at,
            reason=reason if reason in INTERRUPTIONS else DIED,
        )
    try:
        finished = finished_from_mcp(unit, result)
    except CampaignError as error:
        job_id = ""
        if isinstance(result, dict):
            raw_job_id = result.get("job_id")
            job_id = raw_job_id if isinstance(raw_job_id, str) else ""
        return FinishedRecord(
            unit=unit,
            job_id=job_id,
            outcome=Outcome.ERROR,
            at=at,
            reason=str(error),
        )
    return FinishedRecord(
        unit=finished.unit,
        job_id=finished.job_id,
        outcome=finished.outcome,
        at=at,
        failures=(
            tuple(Failure(**raw) for raw in failure_details(result))
            if finished.outcome == Outcome.FAILED
            else ()
        ),
    )
