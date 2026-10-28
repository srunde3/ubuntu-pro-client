"""Reading a behave JSON report: statuses, counts and failing steps."""

from enum import Enum
from typing import Any

from pro_behave.jobs.messages import Failure, ReportSummary, ScenarioStatus


class BehaveStepStatus(str, Enum):
    """A step ``result.status`` value we recognize in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    UNDEFINED = "undefined"


class BehaveScenarioStatus(str, Enum):
    """A scenario ``status`` value we recognize in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    HOOK_ERROR = "hook_error"
    CLEANUP_ERROR = "cleanup_error"
    UNDEFINED = "undefined"
    SKIPPED = "skipped"


_FAILING_STEP_STATUSES = frozenset(
    {
        BehaveStepStatus.FAILED,
        BehaveStepStatus.ERROR,
        BehaveStepStatus.UNDEFINED,
    }
)


_SCENARIO_STATUS_MAP = {
    BehaveScenarioStatus.PASSED: ScenarioStatus.PASSED,
    BehaveScenarioStatus.FAILED: ScenarioStatus.FAILED,
    BehaveScenarioStatus.ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.HOOK_ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.CLEANUP_ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.UNDEFINED: ScenarioStatus.FAILED,
    BehaveScenarioStatus.SKIPPED: ScenarioStatus.SKIPPED,
}


def _behave_step_status(value: Any) -> BehaveStepStatus | None:
    """Parse a raw step status into a recognized member, else None."""
    try:
        return BehaveStepStatus(value)
    except ValueError:
        return None


def _behave_scenario_status(value: Any) -> BehaveScenarioStatus | None:
    """Parse a raw scenario status into a recognized member, else None."""
    try:
        return BehaveScenarioStatus(value)
    except ValueError:
        return None


def scenario_status_from_steps(steps: list[dict[str, Any]]) -> ScenarioStatus:
    statuses = {
        _behave_step_status(step.get("result", {}).get("status"))
        for step in steps
    }
    if statuses & _FAILING_STEP_STATUSES:
        return ScenarioStatus.FAILED
    if statuses == {BehaveStepStatus.SKIPPED}:
        return ScenarioStatus.SKIPPED
    if BehaveStepStatus.PASSED in statuses:
        return ScenarioStatus.PASSED
    return ScenarioStatus.UNKNOWN


def scenario_status_from_element(scenario: dict[str, Any]) -> ScenarioStatus:
    """Classify a report scenario element into passed/failed/skipped/unknown.

    Prefers behave's own scenario-level ``status`` -- the JSON report
    already carries it (it's ``scenario.status.name`` from behave's model,
    the same authoritative value the JUnit reporter's ``testcase.@status``
    would give), so there's no need to infer it. Falls back to
    ``scenario_status_from_steps`` only when that key is absent (e.g.
    hand-built fixtures): a skipped scenario has no executed steps to
    infer a status from, so step-based inference alone always mis-buckets
    it as "unknown" instead of "skipped".
    """
    behave_status = _behave_scenario_status(scenario.get("status"))
    if behave_status is not None:
        return _SCENARIO_STATUS_MAP[behave_status]

    steps = scenario.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    return scenario_status_from_steps(steps)


def summarize_report(report_data: list[Any]) -> ReportSummary:
    summary = {
        "features": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "scenarios": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "steps": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "error": 0,
            "undefined": 0,
            "unknown": 0,
        },
    }
    failures: list[Failure] = []

    for feature in report_data:
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []

        summary["features"]["total"] += 1
        scenario_statuses: list[ScenarioStatus] = []

        for scenario in scenarios:
            scenario_name = str(scenario.get("name", "unknown-scenario"))
            steps = scenario.get("steps", [])
            if not isinstance(steps, list):
                steps = []

            scenario_status = scenario_status_from_element(scenario)
            scenario_statuses.append(scenario_status)
            summary["scenarios"]["total"] += 1
            summary["scenarios"][scenario_status] = (
                summary["scenarios"].get(scenario_status, 0) + 1
            )

            for step in steps:
                step_name = str(step.get("name", "unknown-step"))
                result = step.get("result", {})
                step_status = str(result.get("status", "unknown"))

                summary["steps"]["total"] += 1
                summary["steps"][step_status] = (
                    summary["steps"].get(step_status, 0) + 1
                )

                if _behave_step_status(step_status) in _FAILING_STEP_STATUSES:
                    error_message = str(
                        result.get("error_message", "")
                    ).strip()
                    failures.append(
                        Failure(
                            scenario=scenario_name,
                            step=step_name,
                            status=step_status,
                            error_message=error_message[:2000],
                        )
                    )

        if any(
            status == ScenarioStatus.FAILED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.FAILED
        elif scenario_statuses and all(
            status == ScenarioStatus.SKIPPED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.SKIPPED
        elif any(
            status == ScenarioStatus.PASSED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.PASSED
        else:
            feature_status = ScenarioStatus.UNKNOWN

        summary["features"][feature_status] = (
            summary["features"].get(feature_status, 0) + 1
        )

    return ReportSummary(summary=summary, failures=failures)
