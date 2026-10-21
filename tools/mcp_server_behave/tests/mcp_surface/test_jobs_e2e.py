"""A real behave job, seen and killed from another process.

Nothing about a job lives in the process that started it: its wrapper holds
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

from behave_mcp.config import load_settings
from behave_mcp.server import create_service

FEATURE = "features/_version.feature"
SCENARIO = "Attached show version in a ubuntu machine"

JOB_TIMEOUT_SECONDS = 600
SAMPLE_INTERVAL_SECONDS = 0.5

# Run in a separate interpreter: what a restarted server or the CLI would do.
OTHER_PROCESS = """
import json, os, sys
from behave_mcp.config import load_settings
from behave_mcp.server import create_service

service = create_service(load_settings(os.environ))
job_id = sys.argv[1]
before = service.job_status(job_id).status
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
    service = create_service(load_settings(os.environ))

    started = service.start_scenario(
        FEATURE, SCENARIO, release="jammy", machine_type="lxd-container"
    )
    assert started.status == "started"

    other = subprocess.run(
        [sys.executable, "-c", OTHER_PROCESS, started.job_id],
        env=os.environ,
        capture_output=True,
        text=True,
        check=True,
    )
    seen = json.loads(other.stdout)

    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while service.job_status(started.job_id).status == "running":
        assert time.monotonic() < deadline, "the killed job did not stop"
        time.sleep(SAMPLE_INTERVAL_SECONDS)

    assert seen == {"before": "running", "killed": True}
    ended = service.get_artifacts(started.job_id)
    assert ended.exit is not None
    assert ended.exit.killed is True
    assert ended.exit.ok is False
    assert service.job_status(started.job_id).status == "completed"
