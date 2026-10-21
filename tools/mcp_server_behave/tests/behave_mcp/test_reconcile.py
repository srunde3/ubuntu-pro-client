"""Reconcile across processes: the state directory's lock, real files, real
job processes."""

import subprocess
import sys
import time

import pytest

from behave_mcp.adapters import LocalJobResultStore
from behave_mcp.messages import JobRequest

pytestmark = pytest.mark.integration

RECONCILE = """
import sys
from pathlib import Path
from behave_mcp.wiring import create_service
create_service(state_dir=Path(sys.argv[1])).reconcile()
"""

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

    reconcilers = [
        subprocess.Popen([sys.executable, "-c", RECONCILE, str(tmp_path)])
        for _ in range(RECONCILERS)
    ]
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
    subprocess.run(
        [sys.executable, "-c", RECONCILE, str(tmp_path)], check=True
    )

    assert _eventually(lambda: store.read_exit("second") is not None)
    assert order.read_text().splitlines() == ["first", "second"]
