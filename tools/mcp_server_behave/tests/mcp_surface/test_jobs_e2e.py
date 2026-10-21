"""A real behave job, seen and killed from another process.

Nothing about a job lives in the process that started it: its process holds
the job's lock, and its state is read off disk. So a second process -- a
restarted server, or the CLI -- sees the job running and can kill it, and
the first process sees the kill recorded.

``features/_version.feature``'s attached-version scenario is used because it
is cheap without a contract token: tox still starts, and the job lives long
enough to be killed.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from behave_mcp.server import create_service

FEATURE = "features/_version.feature"
SCENARIO = "Attached show version in a ubuntu machine"

JOB_TIMEOUT_SECONDS = 600
SAMPLE_INTERVAL_SECONDS = 0.5

# Run in a separate interpreter: what a restarted server or the CLI would do.
OTHER_PROCESS = """
import json, os, sys
from behave_mcp.server import create_service

service = create_service()
job_id = sys.argv[1]
before = service.job_status(job_id).state.value
killed = service.kill_job(job_id).killed
print(json.dumps({"before": before, "killed": killed}))
"""


@pytest.mark.e2e
@pytest.mark.long_running
def test_another_process_sees_a_running_job_and_kills_it(
    monkeypatch, tmp_path
):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    service = create_service()

    started = service.run_job(
        FEATURE, SCENARIO, release="jammy", machine_type="lxd-container"
    )
    assert started.state == "running"

    other = subprocess.run(
        [sys.executable, "-c", OTHER_PROCESS, started.job_id],
        env=os.environ,
        capture_output=True,
        text=True,
        check=True,
    )
    seen = json.loads(other.stdout)

    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while service.job_status(started.job_id).state == "running":
        assert time.monotonic() < deadline, "the killed job did not stop"
        time.sleep(SAMPLE_INTERVAL_SECONDS)

    assert seen == {"before": "running", "killed": True}
    ended = service.job_artifacts(started.job_id)
    assert ended.exit is not None
    assert ended.exit.killed is True
    assert ended.exit.ok is False
    interrupted = service.job_status(started.job_id)
    assert (interrupted.state, interrupted.reason) == (
        "interrupted",
        "killed",
    )


@pytest.mark.e2e
@pytest.mark.long_running
def test_two_jobs_on_one_lane_run_back_to_back_with_nobody_calling(
    monkeypatch, tmp_path
):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    service = create_service()

    first = service.run_job(
        FEATURE, SCENARIO, release="jammy", machine_type="lxd-container"
    )
    second = service.run_job(
        FEATURE, SCENARIO, release="noble", machine_type="lxd-container"
    )
    assert (first.state, second.state) == ("running", "queued")

    # Only reads from here on, which never reconcile: the first job's own
    # process must start the second when it ends.
    deadline = time.monotonic() + 2 * JOB_TIMEOUT_SECONDS
    while service.job_status(second.job_id).state != "finished":
        assert time.monotonic() < deadline, "the queued job never ran"
        time.sleep(SAMPLE_INTERVAL_SECONDS)

    first_record = service.job_artifacts(first.job_id)
    second_record = service.job_artifacts(second.job_id)
    assert first_record.exit.finished_at <= second_record.record.started_at
