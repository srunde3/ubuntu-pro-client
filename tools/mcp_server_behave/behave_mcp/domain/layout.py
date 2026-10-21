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
LANES_SUBDIR = "lanes"
# The files in one job's directory, ``jobs/<job_id>/``.
JOB_RECORD_NAME = "job.json"
JOB_EXIT_NAME = "exit.json"
JOB_LOCK_NAME = "job.lock"
# Left by a kill, so a job process still starting up sees it was killed.
JOB_KILL_NAME = "kill"
STDOUT_LOG_NAME = "stdout.log"
JSON_REPORT_NAME = "report.json"
JOB_FILE_NAMES = (
    JOB_RECORD_NAME,
    JOB_EXIT_NAME,
    STDOUT_LOG_NAME,
    JSON_REPORT_NAME,
)
EXPORTS_SUBDIR = "exports"
# The files in one campaign's directory, ``campaigns/<campaign_id>/``.
CAMPAIGN_FILE_NAME = "campaign.json"
CAMPAIGN_RECORDS_NAME = "records.jsonl"
CAMPAIGN_LOCK_NAME = "campaign.lock"


def state_dir(repo_root: Path, configured: Path | None) -> Path:
    """``configured`` (from ``$MCP_STATE_DIR``) if given, else
    ``<repo_root>/.mcp_server_behave``."""
    if configured is not None:
        return configured
    return repo_root / DEFAULT_STATE_DIR_NAME


def campaigns_dir(state: Path) -> Path:
    return state / CAMPAIGNS_SUBDIR


class CampaignPaths(NamedTuple):
    """The directory one campaign owns, and the files in it.

    ``campaign`` holds the campaign record and plan, written once at
    creation; ``records`` is every later record, appended; ``lock`` is the
    run lock.
    """

    campaign_dir: Path
    campaign: Path
    records: Path
    lock: Path


def campaign_paths(campaigns: Path, campaign_id: str) -> CampaignPaths:
    campaign_dir = campaigns / campaign_id
    return CampaignPaths(
        campaign_dir=campaign_dir,
        campaign=campaign_dir / CAMPAIGN_FILE_NAME,
        records=campaign_dir / CAMPAIGN_RECORDS_NAME,
        lock=campaign_dir / CAMPAIGN_LOCK_NAME,
    )


def jobs_dir(state: Path) -> Path:
    return state / JOBS_SUBDIR


def export_file(state: Path, campaign_id: str) -> Path:
    return state / EXPORTS_SUBDIR / (campaign_id + ".tar.gz")


class JobPaths(NamedTuple):
    """The directory one job owns, and the files in it.

    ``record`` is written once at launch and ``exit`` once at exit;
    behave writes ``stdout_log`` and ``json_report`` as it runs.
    """

    job_dir: Path
    record: Path
    exit: Path
    stdout_log: Path
    json_report: Path
    lock: Path
    kill: Path


def job_paths(jobs: Path, job_id: str) -> JobPaths:
    job_dir = jobs / job_id
    return JobPaths(
        job_dir=job_dir,
        record=job_dir / JOB_RECORD_NAME,
        exit=job_dir / JOB_EXIT_NAME,
        stdout_log=job_dir / STDOUT_LOG_NAME,
        json_report=job_dir / JSON_REPORT_NAME,
        lock=job_dir / JOB_LOCK_NAME,
        kill=job_dir / JOB_KILL_NAME,
    )


def lanes_dir(state: Path) -> Path:
    return state / LANES_SUBDIR


def lane_path(lanes: Path, pool: str, lane: int) -> Path:
    """One lane's lock file. Holding its lock is what using the lane means."""
    return lanes / pool / "{}.lock".format(lane)
