"""Pure domain logic for the behave MCP server.

Functions here are free of I/O side effects: they build commands, validate
inputs, and summarize behave JSON reports. Constants shared across modules
also live here.
"""

import posixpath
import re
from pathlib import Path
from typing import Any

from behave_mcp.messages import (
    Artifacts,
    Combo,
    Dimensions,
    DimensionValue,
    Failure,
    FeatureCatalogEntry,
    FeatureDetail,
    ReportSummary,
    ScenarioSummary,
)

ALLOWED_MACHINE_TYPES = {
    "lxd-container",
    "lxd-vm",
    "aws.generic",
    "gcp.generic",
    "azure.generic",
    "aws.pro",
    "gcp.pro",
    "azure.pro",
}
CLOUD_MACHINE_TYPES = {
    "aws.generic",
    "gcp.generic",
    "azure.generic",
    "aws.pro",
    "gcp.pro",
    "azure.pro",
}
ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR = "MCP_ALLOW_CLOUD_MACHINE_TYPES"
MAX_PARALLEL_JOBS_ENV_VAR = "MCP_MAX_PARALLEL_JOBS"
_DEFAULT_RUNNING_TAIL_LINES = 12
_DEFAULT_LOG_TAIL_LINES = 200
_MAX_LOG_TAIL_LINES = 2000
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1800
_DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 5.0
_JOB_INDEX_FILE_NAME = "index.jsonl"
_DEFAULT_MAX_PARALLEL_JOBS = 1


def normalize_feature_file_arg(feature_file: str) -> str:
    normalized = posixpath.normpath(feature_file.replace("\\", "/"))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def validate_machine_types(
    machine_types: list[str], allow_cloud: bool
) -> str | None:
    if not machine_types:
        return (
            "machine_types is required. Allowed values: lxd-container,lxd-vm"
        )

    invalid_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type not in ALLOWED_MACHINE_TYPES
    )
    if invalid_machine_types:
        return (
            "Unsupported machine_types: "
            f"{','.join(invalid_machine_types)}. "
            "Allowed values: lxd-container,lxd-vm"
        )

    cloud_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type in CLOUD_MACHINE_TYPES
    )
    if cloud_machine_types and not allow_cloud:
        return (
            "Cloud machine_types are disabled by default. "
            "Set "
            f"{ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR}=1 "
            "to allow: "
            f"{','.join(cloud_machine_types)}"
        )

    return None


def build_command(
    feature_file: str,
    machine_types: list[str],
    scenario_name: str,
    releases: list[str] | None,
    json_report_path: Path,
) -> list[str]:
    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path), "-f", "plain"])
    return command


def artifacts_payload(
    *,
    log_dir: Path,
    stdout_log: Path,
    json_report: Path,
    metadata: Path,
) -> Artifacts:
    return Artifacts(
        log_dir=str(log_dir),
        stdout_log=str(stdout_log),
        json_report=str(json_report),
        metadata=str(metadata),
    )


def scenario_status_from_steps(steps: list[dict[str, Any]]) -> str:
    statuses = {
        str(step.get("result", {}).get("status", "unknown")) for step in steps
    }
    if statuses & {"failed", "error", "undefined"}:
        return "failed"
    if statuses == {"skipped"}:
        return "skipped"
    if "passed" in statuses:
        return "passed"
    return "unknown"


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
        feature_name = str(feature.get("name", "unknown-feature"))
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []

        summary["features"]["total"] += 1
        scenario_statuses: list[str] = []

        for scenario in scenarios:
            scenario_name = str(scenario.get("name", "unknown-scenario"))
            steps = scenario.get("steps", [])
            if not isinstance(steps, list):
                steps = []

            scenario_status = scenario_status_from_steps(steps)
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

                if step_status in {"failed", "error", "undefined"}:
                    error_message = str(
                        result.get("error_message", "")
                    ).strip()
                    failures.append(
                        Failure(
                            feature=feature_name,
                            scenario=scenario_name,
                            step=step_name,
                            status=step_status,
                            error_message=error_message[:2000],
                        )
                    )

        if any(status == "failed" for status in scenario_statuses):
            feature_status = "failed"
        elif scenario_statuses and all(
            status == "skipped" for status in scenario_statuses
        ):
            feature_status = "skipped"
        elif any(status == "passed" for status in scenario_statuses):
            feature_status = "passed"
        else:
            feature_status = "unknown"

        summary["features"][feature_status] = (
            summary["features"].get(feature_status, 0) + 1
        )

    return ReportSummary(summary=summary, failures=failures)


# ---------------------------------------------------------------------------
# Feature introspection
#
# These functions turn parsed behave model objects (Feature/Scenario/Example)
# into typed DTOs (see ``messages``) and provide the projections and filters
# used by the browse/select MCP tools. They are behave-free: they only rely on
# duck-typed attributes, so tests can pass simple fakes.
# ---------------------------------------------------------------------------

_CONFIG_TAG_PREFIX = "uses.config."
_GIVEN_MACHINE_STEP_RE = re.compile(
    r"a `(.*)` `(.*)` machine with ubuntu-advantage-tools installed"
)


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _literal_or_none(value: str) -> str | None:
    if not value or _is_placeholder(value):
        return None
    return value


def requires_config_from_tags(tags: list[str]) -> list[str]:
    """Return sorted config requirements from ``uses.config.*`` tags."""
    requirements: list[str] = []
    for tag in tags:
        if tag.startswith(_CONFIG_TAG_PREFIX):
            remainder = tag[len(_CONFIG_TAG_PREFIX) :]
            if remainder and remainder not in requirements:
                requirements.append(remainder)
    return sorted(requirements)


def _step_machine_override(scenario: Any) -> tuple[str | None, str | None]:
    steps = getattr(scenario, "steps", None) or []
    if not steps:
        return None, None
    match = _GIVEN_MACHINE_STEP_RE.match(getattr(steps[0], "name", "") or "")
    if not match:
        return None, None
    return _literal_or_none(match.group(1)), _literal_or_none(match.group(2))


def _add_combo(
    combos: list[Combo],
    seen: set[tuple[str, str]],
    release: str | None,
    machine_type: str | None,
) -> None:
    if not release or not machine_type:
        return
    if _is_placeholder(release) or _is_placeholder(machine_type):
        return
    key = (release, machine_type)
    if key in seen:
        return
    seen.add(key)
    combos.append(Combo(release=release, machine_type=machine_type))


def combos_from_scenario(scenario: Any) -> list[Combo]:
    """Return the distinct ``(release, machine_type)`` combos for a scenario.

    Combos come from Scenario Outline ``Examples`` rows, and from a hardcoded
    first step ``Given a `<release>` `<machine_type>` machine ...`` which, when
    literal, overrides the example values (mirroring ``environment.py``).
    """
    step_release, step_machine_type = _step_machine_override(scenario)
    combos: list[Combo] = []
    seen: set[tuple[str, str]] = set()

    examples = getattr(scenario, "examples", None) or []
    if examples:
        for example in examples:
            table = getattr(example, "table", None)
            if table is None:
                continue
            headings = list(getattr(table, "headings", []) or [])
            for row in getattr(table, "rows", []) or []:
                row_map = dict(zip(headings, list(getattr(row, "cells", []))))
                _add_combo(
                    combos,
                    seen,
                    step_release or row_map.get("release"),
                    step_machine_type or row_map.get("machine_type"),
                )
    else:
        _add_combo(combos, seen, step_release, step_machine_type)

    return combos


def _example_columns(scenario: Any) -> list[str]:
    columns: list[str] = []
    for example in getattr(scenario, "examples", None) or []:
        table = getattr(example, "table", None)
        if table is None:
            continue
        for heading in getattr(table, "headings", []) or []:
            if heading not in columns:
                columns.append(heading)
    return columns


def summarize_feature(feature: Any) -> FeatureDetail:
    """Turn a parsed behave ``Feature`` into a ``FeatureDetail`` DTO.

    ``path`` is left empty here; the adapter fills it in per file.
    """
    feature_tags = list(getattr(feature, "tags", []) or [])
    scenarios: list[ScenarioSummary] = []
    for scenario in getattr(feature, "scenarios", []) or []:
        scenario_tags = list(getattr(scenario, "tags", []) or [])
        effective_tags = feature_tags + scenario_tags
        scenarios.append(
            ScenarioSummary(
                name=getattr(scenario, "name", "") or "",
                type=getattr(scenario, "type", "scenario"),
                tags=scenario_tags,
                requires_config=requires_config_from_tags(effective_tags),
                example_columns=_example_columns(scenario),
                combos=combos_from_scenario(scenario),
            )
        )
    return FeatureDetail(
        path="",
        title=getattr(feature, "name", "") or "",
        tags=feature_tags,
        requires_config=requires_config_from_tags(feature_tags),
        scenarios=scenarios,
    )


def catalog_entry(feature_detail: FeatureDetail) -> FeatureCatalogEntry:
    """Project a full feature detail into a lightweight catalog entry."""
    releases: list[str] = []
    machine_types: list[str] = []
    requires_config = list(feature_detail.requires_config)
    for scenario in feature_detail.scenarios:
        for requirement in scenario.requires_config:
            if requirement not in requires_config:
                requires_config.append(requirement)
        for combo in scenario.combos:
            if combo.release not in releases:
                releases.append(combo.release)
            if combo.machine_type not in machine_types:
                machine_types.append(combo.machine_type)
    return FeatureCatalogEntry(
        path=feature_detail.path,
        title=feature_detail.title,
        scenario_count=len(feature_detail.scenarios),
        requires_config=sorted(requires_config),
        releases=sorted(releases),
        machine_types=sorted(machine_types),
    )


def filtered_combos(
    scenario: ScenarioSummary,
    release: str | None = None,
    machine_type: str | None = None,
) -> list[Combo]:
    """Return the scenario combos matching the given release/machine_type."""
    result: list[Combo] = []
    for combo in scenario.combos:
        if release is not None and combo.release != release:
            continue
        if machine_type is not None and combo.machine_type != machine_type:
            continue
        result.append(combo)
    return result


def scenario_matches(
    scenario: ScenarioSummary,
    feature_tags: list[str],
    *,
    release: str | None = None,
    machine_type: str | None = None,
    tag: str | None = None,
    text: str | None = None,
) -> bool:
    """Return whether a scenario satisfies all provided filters."""
    if (release is not None or machine_type is not None) and not (
        filtered_combos(scenario, release, machine_type)
    ):
        return False
    if tag is not None:
        all_tags = set(feature_tags) | set(scenario.tags)
        if tag not in all_tags:
            return False
    if text is not None:
        if text.lower() not in scenario.name.lower():
            return False
    return True


def aggregate_dimensions(
    feature_details: list[FeatureDetail],
) -> Dimensions:
    """Return distinct releases and machine_types with scenario counts.

    Each scenario contributes at most once to a given release or machine_type,
    regardless of how many example rows reference it.
    """
    release_counts: dict[str, int] = {}
    machine_type_counts: dict[str, int] = {}
    for feature_detail in feature_details:
        for scenario in feature_detail.scenarios:
            releases_seen: set[str] = set()
            machine_types_seen: set[str] = set()
            for combo in scenario.combos:
                releases_seen.add(combo.release)
                machine_types_seen.add(combo.machine_type)
            for release in releases_seen:
                release_counts[release] = release_counts.get(release, 0) + 1
            for machine_type in machine_types_seen:
                machine_type_counts[machine_type] = (
                    machine_type_counts.get(machine_type, 0) + 1
                )
    return Dimensions(
        releases=[
            DimensionValue(name=name, scenario_count=count)
            for name, count in sorted(release_counts.items())
        ],
        machine_types=[
            DimensionValue(name=name, scenario_count=count)
            for name, count in sorted(machine_type_counts.items())
        ],
    )
