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
JOB_INDEX_FILE_NAME = "index.jsonl"
STDOUT_LOG_SUFFIX = "_stdout.log"
JSON_REPORT_SUFFIX = "_report.json"
METADATA_SUFFIX = "_meta.json"
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


class JobArtifactPaths(NamedTuple):
    """The three on-disk files a job owns, named from its id."""

    stdout_log: Path
    json_report: Path
    metadata: Path


def job_artifact_paths(log_dir: Path, job_id: str) -> JobArtifactPaths:
    return JobArtifactPaths(
        stdout_log=log_dir / f"{job_id}{STDOUT_LOG_SUFFIX}",
        json_report=log_dir / f"{job_id}{JSON_REPORT_SUFFIX}",
        metadata=log_dir / f"{job_id}{METADATA_SUFFIX}",
    )
