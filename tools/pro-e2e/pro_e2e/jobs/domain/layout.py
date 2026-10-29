"""How the server's state is laid out on disk, so every front-end finds the
same file.

Pure naming: callers resolve ``PRO_E2E_STATE_DIR`` and pass the result in.
Shared by ``pro_e2e.jobs`` and ``pro_e2e.campaign``; nothing here imports
either.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

REPO_ENV_VAR = "UBUNTU_PRO_CLIENT_REPO"
STATE_DIR_ENV_VAR = "PRO_E2E_STATE_DIR"
DEFAULT_STATE_DIR_NAME = ".pro-e2e"
JOBS_SUBDIR = "jobs"
CAMPAIGNS_SUBDIR = "campaigns"
LANES_SUBDIR = "lanes"
# The service's config for this checkout.
CONFIG_NAME = "config.json"
# Held while reconcile runs, by whichever process runs it.
RECONCILE_LOCK_NAME = "reconcile.lock"
# The files in one job's directory, ``jobs/<job_id>/``.
JOB_REQUEST_NAME = "request.json"
JOB_RECORD_NAME = "job.json"
JOB_RESULT_NAME = "result.json"
JOB_LOCK_NAME = "job.lock"
# Left by a kill, so a job process still starting up sees it was killed.
JOB_KILL_NAME = "kill"
STDOUT_LOG_NAME = "stdout.log"
JSON_REPORT_NAME = "report.json"
# The files that are a job's evidence; its request is repeated in its record.
JOB_FILE_NAMES = (
    JOB_RECORD_NAME,
    JOB_RESULT_NAME,
    STDOUT_LOG_NAME,
    JSON_REPORT_NAME,
)
EXPORTS_SUBDIR = "exports"
# The files in one campaign's directory, ``campaigns/<campaign_id>/``.
CAMPAIGN_FILE_NAME = "campaign.json"
CAMPAIGN_RECORDS_NAME = "records.jsonl"


def state_dir(repo_root: Path, configured: Path | None) -> Path:
    """``configured`` (from ``$PRO_E2E_STATE_DIR``) if given, else
    ``<repo_root>/.pro-e2e``."""
    if configured is not None:
        return configured
    return repo_root / DEFAULT_STATE_DIR_NAME


def campaigns_dir(state: Path) -> Path:
    return state / CAMPAIGNS_SUBDIR


class CampaignPaths(NamedTuple):
    """The directory one campaign owns, and the files in it.

    ``campaign`` holds the campaign record and plan, written once at
    creation; ``records`` is every later record, appended.
    """

    campaign_dir: Path
    campaign: Path
    records: Path


def campaign_paths(campaigns: Path, campaign_id: str) -> CampaignPaths:
    campaign_dir = campaigns / campaign_id
    return CampaignPaths(
        campaign_dir=campaign_dir,
        campaign=campaign_dir / CAMPAIGN_FILE_NAME,
        records=campaign_dir / CAMPAIGN_RECORDS_NAME,
    )


def jobs_dir(state: Path) -> Path:
    return state / JOBS_SUBDIR


def export_file(state: Path, campaign_id: str) -> Path:
    return state / EXPORTS_SUBDIR / (campaign_id + ".tar.gz")


class JobPaths(NamedTuple):
    """The directory one job owns, and the files in it.

    ``request`` is written once when the job is queued, ``record`` once at
    launch and ``result`` once when it ends; the command writes
    ``stdout_log`` and ``json_report`` as it runs.
    """

    job_dir: Path
    request: Path
    record: Path
    result: Path
    stdout_log: Path
    json_report: Path
    lock: Path
    kill: Path


def job_paths(jobs: Path, job_id: str) -> JobPaths:
    job_dir = jobs / job_id
    return JobPaths(
        job_dir=job_dir,
        request=job_dir / JOB_REQUEST_NAME,
        record=job_dir / JOB_RECORD_NAME,
        result=job_dir / JOB_RESULT_NAME,
        stdout_log=job_dir / STDOUT_LOG_NAME,
        json_report=job_dir / JSON_REPORT_NAME,
        lock=job_dir / JOB_LOCK_NAME,
        kill=job_dir / JOB_KILL_NAME,
    )


def config_file(state: Path) -> Path:
    return state / CONFIG_NAME


def reconcile_lock(state: Path) -> Path:
    return state / RECONCILE_LOCK_NAME


def lanes_dir(state: Path) -> Path:
    return state / LANES_SUBDIR


def lane_path(lanes: Path, pool: str, lane: int) -> Path:
    """One lane's lock file. Holding its lock is what using the lane means."""
    return lanes / pool / "{}.lock".format(lane)
