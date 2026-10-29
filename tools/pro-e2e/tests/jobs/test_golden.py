"""Golden byte-shape tests for the serializers used by the job service.

These guard the exact on-disk bytes of a job's record and result, plus the
JSON key ordering of tool response payloads.
"""

import pytest

from pro_e2e.jobs.adapters import (
    LocalJobResultStore,
    LocalJobResultStoreFactory,
)
from pro_e2e.jobs.domain.results import (
    killed_result,
    start_failure,
    unreadable_result,
)
from pro_e2e.jobs.messages import (
    Failure,
    InstanceResult,
    InstanceStatus,
    JobRecord,
    JobRequest,
    JobResult,
    Outcome,
)
from pro_e2e.jobs.service import JobService
from tests.conftest import (
    FakeJobLauncher,
    FakeWorkspace,
    job_file,
    make_service,
)


def test_write_record_byte_shape(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    store.write_record("jobshape", JobRecord(job_id="jobshape"))
    path = tmp_path / "jobshape" / "job.json"
    expected = (
        "{\n"
        '  "artifacts": null,\n'
        '  "campaign_id": "",\n'
        '  "command": [],\n'
        '  "config": {},\n'
        '  "feature": "",\n'
        '  "install_from": "",\n'
        '  "job_id": "jobshape",\n'
        '  "machine_type": "",\n'
        '  "release": "",\n'
        '  "repo": {\n'
        '    "branch": null,\n'
        '    "commit": null,\n'
        '    "dirty": null,\n'
        '    "root": ""\n'
        "  },\n"
        '  "scenario": "",\n'
        '  "started_at": null\n'
        "}\n"
    )
    assert path.read_text(encoding="utf-8") == expected


PASSED_WITH_A_SKIP = JobResult(
    finished_at="T1",
    returncode=0,
    outcome=Outcome.PASSED,
    instances=[
        InstanceResult(
            id="features/a.feature:12",
            name="A -- @1.1 rows",
            status=InstanceStatus.PASSED,
        ),
        InstanceResult(
            id="features/a.feature:13",
            name="A -- @1.2 rows",
            status=InstanceStatus.SKIPPED,
            reason="no token",
        ),
    ],
)

FAILED = JobResult(
    finished_at="T1",
    returncode=1,
    outcome=Outcome.FAILED,
    instances=[
        InstanceResult(
            id="features/a.feature:12",
            name="A",
            status=InstanceStatus.FAILED,
        )
    ],
    failures=[
        Failure(
            instance="features/a.feature:12",
            step="Then it works",
            status="failed",
            error_message="it did not",
        )
    ],
)

# Each form a result.json takes, and its exact bytes.
RESULT_BYTES = {
    "passed-with-a-skip": (
        PASSED_WITH_A_SKIP,
        "{\n"
        '  "failures": [],\n'
        '  "finished_at": "T1",\n'
        '  "instances": [\n'
        "    {\n"
        '      "id": "features/a.feature:12",\n'
        '      "name": "A -- @1.1 rows",\n'
        '      "reason": "",\n'
        '      "status": "passed"\n'
        "    },\n"
        "    {\n"
        '      "id": "features/a.feature:13",\n'
        '      "name": "A -- @1.2 rows",\n'
        '      "reason": "no token",\n'
        '      "status": "skipped"\n'
        "    }\n"
        "  ],\n"
        '  "killed": false,\n'
        '  "outcome": "passed",\n'
        '  "reason": "",\n'
        '  "returncode": 0\n'
        "}\n",
    ),
    "failed": (
        FAILED,
        "{\n"
        '  "failures": [\n'
        "    {\n"
        '      "error_message": "it did not",\n'
        '      "instance": "features/a.feature:12",\n'
        '      "status": "failed",\n'
        '      "step": "Then it works"\n'
        "    }\n"
        "  ],\n"
        '  "finished_at": "T1",\n'
        '  "instances": [\n'
        "    {\n"
        '      "id": "features/a.feature:12",\n'
        '      "name": "A",\n'
        '      "reason": "",\n'
        '      "status": "failed"\n'
        "    }\n"
        "  ],\n"
        '  "killed": false,\n'
        '  "outcome": "failed",\n'
        '  "reason": "",\n'
        '  "returncode": 1\n'
        "}\n",
    ),
    "killed": (
        killed_result("T1", returncode=-15),
        "{\n"
        '  "failures": [],\n'
        '  "finished_at": "T1",\n'
        '  "instances": [],\n'
        '  "killed": true,\n'
        '  "outcome": null,\n'
        '  "reason": "",\n'
        '  "returncode": -15\n'
        "}\n",
    ),
    "could-not-start": (
        start_failure("T1", "Could not start: no tox"),
        "{\n"
        '  "failures": [],\n'
        '  "finished_at": "T1",\n'
        '  "instances": [],\n'
        '  "killed": false,\n'
        '  "outcome": "error",\n'
        '  "reason": "Could not start: no tox",\n'
        '  "returncode": null\n'
        "}\n",
    ),
    "unreadable": (
        unreadable_result("T1", 0, ValueError("bad json")),
        "{\n"
        '  "failures": [],\n'
        '  "finished_at": "T1",\n'
        '  "instances": [],\n'
        '  "killed": false,\n'
        '  "outcome": "error",\n'
        '  "reason": "could not read the job\'s result: '
        "ValueError('bad json')\",\n"
        '  "returncode": 0\n'
        "}\n",
    ),
}


@pytest.mark.parametrize(
    "result, expected", RESULT_BYTES.values(), ids=list(RESULT_BYTES)
)
def test_write_result_byte_shape(tmp_path, result, expected):
    store = LocalJobResultStoreFactory().bind(tmp_path)

    store.write_result("jobshape", result)

    path = tmp_path / "jobshape" / "result.json"
    assert path.read_text(encoding="utf-8") == expected


def _service(tmp_path, launcher, monotonic=lambda: 0.0) -> JobService:
    return make_service(
        FakeWorkspace(repo_root=tmp_path, jobs_dir=tmp_path),
        launcher=launcher,
        monotonic=monotonic,
    )


def _launched(tmp_path, job_id):
    """A job's request and record, as the service writes them."""
    store = LocalJobResultStore(tmp_path)
    store.write_request(job_id, JobRequest(job_id=job_id))
    store.write_record(job_id, JobRecord(job_id=job_id))
    launcher = FakeJobLauncher(tmp_path)
    launcher.running.add(job_id)
    return launcher


# What a finished job's response carries, in order.
FINISHED_KEYS = [
    "job_id",
    "state",
    "feature",
    "scenario",
    "release",
    "machine_type",
    "outcome",
    "reason",
    "instances",
    "failures",
    "returncode",
    "artifacts",
    "recent_output",
]


@pytest.mark.parametrize("outcome", list(Outcome), ids=str)
def test_finished_key_order(tmp_path, outcome):
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "done\n", encoding="utf-8"
    )
    launcher = _launched(tmp_path, job_id)
    launcher.finish(job_id, outcome)

    payload = (
        _service(tmp_path, launcher)
        .wait_for_job(job_id, max_wait_seconds=5, poll_interval_seconds=0.01)
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == FINISHED_KEYS
    # The log's tail comes along only when the job established nothing.
    assert (payload["recent_output"] is not None) is (outcome == Outcome.ERROR)


def test_timed_out_key_order(tmp_path):
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "running\n", encoding="utf-8"
    )
    launcher = _launched(tmp_path, job_id)

    values = iter([0.0, 1.1])
    service = _service(tmp_path, launcher, monotonic=lambda: next(values))

    payload = service.wait_for_job(
        job_id, max_wait_seconds=1, poll_interval_seconds=0.01
    ).model_dump(mode="json")

    assert list(payload.keys()) == [
        "state",
        "job_id",
        "started_at",
        "recent_output",
        "timed_out",
        "artifacts",
    ]
    assert (payload["state"], payload["timed_out"]) == ("running", True)
