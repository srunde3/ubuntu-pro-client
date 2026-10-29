"""Reading a behave job: each run its JSON report shows, and its log's
skip reasons and hook errors."""

import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Sequence

from pro_e2e.behave.logs import digest_log
from pro_e2e.jobs.domain.results import ObservedInstance
from pro_e2e.jobs.messages import Failure, InstanceStatus

# behave's scenario statuses: a failing step is a failure of the product;
# anything else that is not a pass or a skip means the test machinery broke.
STATUS_OF = {
    "passed": InstanceStatus.PASSED,
    "failed": InstanceStatus.FAILED,
    "error": InstanceStatus.FAILED,
    "skipped": InstanceStatus.SKIPPED,
}

# The step statuses a failing step reports.
FAILING_STEP_STATUSES = frozenset({"failed", "error", "undefined"})

MAX_ERROR_MESSAGE_CHARS = 2000

# behave logs why it skipped each run: "SKIP Scenario <name>: <reason>".
_SKIP_LINE = re.compile(r"SKIP Scenario (?P<rest>.*)$")


def read_report(path: Path) -> list[Any] | None:
    """behave's JSON report, or None if it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def read_log(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.rstrip("\n").split("\n") if text else []


def _skip_reasons(
    log: Sequence[str], names: Sequence[str]
) -> dict[str, deque[str]]:
    """Each run's skip reasons, in the order behave logged them."""
    reasons: dict[str, deque[str]] = defaultdict(deque)
    known = sorted(set(names), key=len, reverse=True)
    for line in log:
        match = _SKIP_LINE.search(line)
        if not match:
            continue
        rest = match.group("rest")
        for name in known:
            if rest.startswith(name + ": "):
                reasons[name].append(rest[len(name) + 2 :])
                break
    return reasons


def _failures(instance: str, element: dict[str, Any]) -> list[Failure]:
    failures = []
    for step in element.get("steps") or []:
        result = step.get("result") or {}
        status = str(result.get("status", ""))
        if status in FAILING_STEP_STATUSES:
            failures.append(
                Failure(
                    instance=instance,
                    step=str(step.get("name", "")),
                    status=status,
                    error_message=str(result.get("error_message", ""))[
                        :MAX_ERROR_MESSAGE_CHARS
                    ].strip(),
                )
            )
    return failures


def observe(report: Path, log: Path) -> list[ObservedInstance] | None:
    """Every run the report shows, as the tool's observations.

    A run is identified by its location, ``<feature>:<line>``. A run whose
    status says the test machinery broke takes its reason from the log's
    first hook error, when it shows one.
    """
    data = read_report(report)
    if data is None:
        return None
    elements = [
        element
        for feature in data
        if isinstance(feature, dict)
        for element in feature.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "scenario"
    ]
    lines = read_log(log)
    skips = _skip_reasons(lines, [str(e.get("name", "")) for e in elements])
    hook_errors = [
        region.exception
        for region in digest_log(lines).errors
        if region.kind == "hook_error"
    ]
    observed = []
    for element in elements:
        name = str(element.get("name", ""))
        id = str(element.get("location", "")).removeprefix("./")
        raw = element.get("status")
        status = STATUS_OF.get(str(raw), InstanceStatus.ERROR)
        reason = ""
        if status == InstanceStatus.SKIPPED and skips[name]:
            reason = skips[name].popleft()
        elif status == InstanceStatus.ERROR:
            reason = (
                hook_errors[0]
                if hook_errors
                else "behave status {}".format(raw)
            )
        observed.append(
            ObservedInstance(
                id=id,
                name=name,
                status=status,
                reason=reason,
                failures=tuple(_failures(id, element)),
            )
        )
    return observed
