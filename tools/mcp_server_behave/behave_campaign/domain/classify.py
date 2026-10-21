"""What a finished MCP job established for its unit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from behave_campaign.domain.records import (
    AttemptFinished,
    CampaignError,
    Outcome,
    Unit,
    text_field,
)

MCP_RUNNING_STATUSES = ("started", "timeout")


MCP_COMPLETED = "completed"


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


def finished_from_mcp(unit: Unit, result: Any) -> AttemptFinished:
    """Map one completed MCP job payload onto a finished attempt.

    Only a completed job has an outcome. Handing a job still in flight here
    means something is being recorded that nobody has an outcome for yet.
    """
    if not isinstance(result, dict):
        raise CampaignError("each MCP result must be a JSON object")

    status = result.get("status")
    if status != MCP_COMPLETED:
        raise CampaignError(
            "cannot record an outcome for MCP status '{}'; only a "
            "completed job has one".format(status)
        )
    if "job_id" not in result:
        raise CampaignError("MCP result is missing 'job_id'")

    return AttemptFinished(
        unit=unit,
        job_id=text_field(result, "job_id", "MCP result"),
        outcome=_completed_outcome(result),
    )


@dataclass(frozen=True)
class Classification:
    """One finished job mapped onto an attempt.

    ``problem`` is set when the job's result could not be classified. The
    outcome is still recorded, as ``error``, because a scheduler has nobody
    to raise at and one strange job must not stop a campaign.
    """

    finished: AttemptFinished
    problem: str = ""


def classify_result(unit: Unit, result: Any, at: str = "") -> Classification:
    """Map a finished MCP job onto an outcome. Never raises."""
    try:
        finished = finished_from_mcp(unit, result)
    except CampaignError as error:
        job_id = ""
        if isinstance(result, dict):
            raw_job_id = result.get("job_id")
            job_id = raw_job_id if isinstance(raw_job_id, str) else ""
        return Classification(
            finished=AttemptFinished(
                unit=unit,
                job_id=job_id,
                outcome=Outcome.ERROR,
                at=at,
            ),
            problem=str(error),
        )
    return Classification(
        finished=AttemptFinished(
            unit=finished.unit,
            job_id=finished.job_id,
            outcome=finished.outcome,
            at=at,
        )
    )
