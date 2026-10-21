"""How the server's state is laid out on disk, so every front-end finds the
same file.

Pure naming: callers resolve ``MCP_STATE_DIR`` and pass the result in.
Shared by ``behave_mcp`` and ``behave_campaign``; nothing here imports
either.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

REPO_ENV_VAR = "UBUNTU_PRO_CLIENT_REPO"
STATE_DIR_ENV_VAR = "MCP_STATE_DIR"
DEFAULT_STATE_DIR_NAME = ".mcp_server_behave"
JOBS_SUBDIR = "jobs"
CAMPAIGNS_SUBDIR = "campaigns"
# The files in one job's directory, ``jobs/<job_id>/``.
JOB_RECORD_NAME = "job.json"
JOB_STATUS_NAME = "status.json"
STDOUT_LOG_NAME = "stdout.log"
JSON_REPORT_NAME = "report.json"
JOB_FILE_NAMES = (
    JOB_RECORD_NAME,
    JOB_STATUS_NAME,
    STDOUT_LOG_NAME,
    JSON_REPORT_NAME,
)
EXPORTS_SUBDIR = "exports"
CAMPAIGN_SUFFIX = ".jsonl"


def state_dir(repo_root: Path, configured: Path | None) -> Path:
    """``configured`` (from ``$MCP_STATE_DIR``) if given, else
    ``<repo_root>/.mcp_server_behave``."""
    if configured is not None:
        return configured
    return repo_root / DEFAULT_STATE_DIR_NAME


def campaign_dir(state: Path) -> Path:
    return state / CAMPAIGNS_SUBDIR


def campaign_file(state: Path, campaign_id: str) -> Path:
    return campaign_dir(state) / (campaign_id + CAMPAIGN_SUFFIX)


def jobs_dir(state: Path) -> Path:
    return state / JOBS_SUBDIR


def export_file(state: Path, campaign_id: str) -> Path:
    return state / EXPORTS_SUBDIR / (campaign_id + ".tar.gz")


class JobPaths(NamedTuple):
    """The directory one job owns, and the files in it.

    ``record`` is written once at launch and ``status`` once at exit;
    behave writes ``stdout_log`` and ``json_report`` as it runs.
    """

    job_dir: Path
    record: Path
    status: Path
    stdout_log: Path
    json_report: Path


def job_paths(jobs: Path, job_id: str) -> JobPaths:
    job_dir = jobs / job_id
    return JobPaths(
        job_dir=job_dir,
        record=job_dir / JOB_RECORD_NAME,
        status=job_dir / JOB_STATUS_NAME,
        stdout_log=job_dir / STDOUT_LOG_NAME,
        json_report=job_dir / JSON_REPORT_NAME,
    )
