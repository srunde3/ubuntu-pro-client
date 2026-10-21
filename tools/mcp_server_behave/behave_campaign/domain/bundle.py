"""What a complete campaign's evidence bundle holds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from behave_campaign.domain.records import UnitStatus
from behave_mcp.domain import layout

SUMMARY_NAME = "SUMMARY.md"


CAMPAIGN_JSON_NAME = "campaign.json"


UNITS_CSV_NAME = "units.csv"


JOBS_DIR = "jobs"


AUDIT_DIR = "audit"


# The campaign's own files, copied under ``audit/`` as stored.
AUDIT_FILES = (layout.CAMPAIGN_RECORD_NAME, layout.CAMPAIGN_LOG_NAME)


# The files one job leaves behind. Each keeps its name in the bundle.
JOB_FILES = layout.JOB_FILE_NAMES


@dataclass(frozen=True)
class BundleEntry:
    """One file in the bundle: bytes rendered here, or a file copied as is.

    ``name`` is relative to the bundle's top directory.
    """

    name: str
    data: bytes | None = None
    source: Path | None = None


def job_ids(statuses: Sequence[UnitStatus]) -> list[str]:
    """Every attempt's job id, final or superseded, in a stable order."""
    ids = {
        attempt.job_id for status in statuses for attempt in status.attempts
    }
    return sorted(ids)
