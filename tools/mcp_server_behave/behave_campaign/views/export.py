"""The evidence bundle's files for a reviewer: summary, JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, Sequence

from behave_campaign.domain.records import (
    CampaignHeader,
    UnitState,
    UnitStatus,
    lanes_as_dict,
)
from behave_campaign.domain.state import count_states

UNITS_CSV_COLUMNS = (
    "feature",
    "scenario",
    "release",
    "machine_type",
    "state",
    "job_id",
    "finished_at",
)


def ordered_releases(
    releases: Iterable[str], release_order: Sequence[str] = ()
) -> list[str]:
    """Releases in the campaign's scope order, then any others sorted."""
    present = set(releases)
    first = [release for release in release_order if release in present]
    return first + sorted(present - set(first))


def completed_at(statuses: Sequence[UnitStatus]) -> str:
    """When the last attempt finished."""
    return max(
        (
            attempt.finished_at
            for status in statuses
            for attempt in status.attempts
            if attempt.finished_at
        ),
        default="",
    )


def _final_attempt_finished_at(status: UnitStatus) -> str:
    for attempt in status.attempts:
        if attempt.job_id == status.job_id:
            return attempt.finished_at
    return ""


def render_units_csv(statuses: Sequence[UnitStatus]) -> str:
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream, fieldnames=UNITS_CSV_COLUMNS, lineterminator="\n"
    )
    writer.writeheader()
    for status in sorted(statuses, key=lambda s: s.unit):
        writer.writerow(
            {
                **status.unit.as_dict(),
                "state": status.state,
                "job_id": status.job_id or "",
                "finished_at": _final_attempt_finished_at(status),
            }
        )
    return stream.getvalue()


def render_campaign_json(
    campaign_id: str,
    header: CampaignHeader,
    lifecycle: str,
    statuses: Sequence[UnitStatus],
) -> str:
    """Scope and outcome; no repo state, as no commit pins the jobs."""
    document = {
        "campaign_id": campaign_id,
        "created_at": header.at,
        "completed_at": completed_at(statuses),
        "lifecycle": lifecycle,
        "install_from": header.install_from,
        "lanes": lanes_as_dict(header.lanes),
        "scope": header.filters.scope_as_dict(),
        "total_units": len(statuses),
        "counts": count_states(statuses),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _scope_line(header: CampaignHeader, statuses: Sequence[UnitStatus]) -> str:
    releases = ordered_releases(
        {s.unit.release for s in statuses}, header.filters.release
    )
    machine_types = sorted({s.unit.machine_type for s in statuses})
    features = sorted({s.unit.feature for s in statuses})
    return (
        "Scope: {} releases ({}); {} machine types ({}); {} features".format(
            len(releases),
            ", ".join(releases),
            len(machine_types),
            ", ".join(machine_types),
            len(features),
        )
    )


def _counts_line(statuses: Sequence[UnitStatus]) -> str:
    counts = count_states(statuses)
    return "Units: {}. passed {}, failed {}, error {}, skipped {}".format(
        len(statuses),
        counts[UnitState.PASSED],
        counts[UnitState.FAILED],
        counts[UnitState.ERROR],
        counts[UnitState.SKIPPED],
    )


def _unit_lines(
    statuses: Sequence[UnitStatus],
    release_order: Sequence[str],
    missing_jobs: set[str],
    with_state: bool,
) -> list[str]:
    """Feature, then scenario, then one line per release."""
    grouped: dict[str, dict[str, dict[str, list[UnitStatus]]]] = {}
    for status in statuses:
        unit = status.unit
        grouped.setdefault(unit.feature, {}).setdefault(
            unit.scenario, {}
        ).setdefault(unit.release, []).append(status)

    lines: list[str] = []
    for feature in sorted(grouped):
        lines += ["", "### {}".format(feature)]
        for scenario in sorted(grouped[feature]):
            lines += ["", "#### {}".format(scenario)]
            by_release = grouped[feature][scenario]
            for release in ordered_releases(by_release, release_order):
                entries = []
                for status in sorted(
                    by_release[release], key=lambda s: s.unit.machine_type
                ):
                    notes = []
                    if with_state:
                        notes += [
                            str(status.state),
                            "job {}".format(status.job_id),
                        ]
                    if status.job_id in missing_jobs:
                        notes.append("files missing")
                    entry = status.unit.machine_type
                    if notes:
                        entry += " ({})".format(", ".join(notes))
                    entries.append(entry)
                lines.append("- {}: {}".format(release, ", ".join(entries)))
    return lines


def render_summary(
    campaign_id: str,
    header: CampaignHeader,
    statuses: Sequence[UnitStatus],
    missing_jobs: Iterable[str] = (),
) -> str:
    """SUMMARY.md: what ran and what passed."""
    release_order = header.filters.release
    missing = set(missing_jobs)
    passed = [s for s in statuses if s.state == UnitState.PASSED]
    not_passed = [s for s in statuses if s.state != UnitState.PASSED]

    lines = [
        "# SRU verification evidence: {}".format(campaign_id),
        "",
        "Completed {}. install_from: {}".format(
            completed_at(statuses) or "unknown", header.install_from
        ),
        _scope_line(header, statuses),
        _counts_line(statuses),
        "",
        "## Passed ({})".format(len(passed)),
        *_unit_lines(passed, release_order, missing, with_state=False),
        "",
        "## Not passed ({})".format(len(not_passed)),
        *_unit_lines(not_passed, release_order, missing, with_state=True),
    ]
    return "\n".join(lines) + "\n"
