"""Reconcile across processes: the state directory's lock, real files, real
job processes."""

import os
import subprocess
import sys
import time

import pytest

from behave_mcp.adapters import LocalJobResultStore
from behave_mcp.messages import JobRequest

pytestmark = pytest.mark.integration

# Each entry point builds its own job service. One reconcile from each, run
# in its own process, the way that entry point runs it.
RECONCILE_BY = {
    "server": [
        "-c",
        "from behave_mcp.server import create_service\n"
        "create_service().reconcile()",
    ],
    # Any command reconciles; config is the cheapest.
    "cli": ["-m", "behave_campaign.cli", "config", "--repo-root", "."],
}


def _reconcile(tmp_path, entry_point="cli"):
    return subprocess.Popen(
        [sys.executable, *RECONCILE_BY[entry_point]],
        cwd=tmp_path,
        env={**os.environ, "MCP_STATE_DIR": str(tmp_path)},
        stdout=subprocess.DEVNULL,
    )


# Two processes reconciling at once; each tries to start every queued job.
RECONCILERS = 2


def _eventually(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_two_reconciles_at_once_start_a_queued_job_once(tmp_path):
    starts = tmp_path / "starts"
    LocalJobResultStore(tmp_path / "jobs").write_request(
        "job",
        JobRequest(
            job_id="job",
            queued_at="T0",
            machine_type="lxd-container",
            command=["sh", "-c", "echo started >> {}".format(starts)],
            repo_root=str(tmp_path),
        ),
    )

    reconcilers = [_reconcile(tmp_path) for _ in range(RECONCILERS)]
    for reconciler in reconcilers:
        assert reconciler.wait(timeout=30) == 0

    store = LocalJobResultStore(tmp_path / "jobs")
    assert _eventually(lambda: store.read_exit("job") is not None)
    assert starts.read_text().splitlines() == ["started"]


def test_a_job_that_ends_starts_the_next_queued_job_itself(tmp_path):
    order = tmp_path / "order"
    store = LocalJobResultStore(tmp_path / "jobs")
    for number, job_id in enumerate(["first", "second"]):
        store.write_request(
            job_id,
            JobRequest(
                job_id=job_id,
                queued_at="T{}".format(number),
                machine_type="lxd-container",
                command=["sh", "-c", "echo {} >> {}".format(job_id, order)],
                repo_root=str(tmp_path),
            ),
        )

    # One lane by default, so this starts only the first job. Nothing else
    # reconciles: the first job's process starts the second.
    assert _reconcile(tmp_path).wait(timeout=30) == 0

    assert _eventually(lambda: store.read_exit("second") is not None)
    assert order.read_text().splitlines() == ["first", "second"]


@pytest.mark.parametrize("entry_point", RECONCILE_BY)
def test_every_entry_point_starts_what_is_queued(tmp_path, entry_point):
    # The job process is the third entry point; the test above proves it.
    started = tmp_path / "started"
    store = LocalJobResultStore(tmp_path / "jobs")
    store.write_request(
        "job",
        JobRequest(
            job_id="job",
            queued_at="T0",
            machine_type="lxd-container",
            command=["sh", "-c", "echo started > {}".format(started)],
            repo_root=str(tmp_path),
        ),
    )

    assert _reconcile(tmp_path, entry_point).wait(timeout=60) == 0

    assert _eventually(lambda: store.read_exit("job") is not None)
    assert started.read_text().strip() == "started"
