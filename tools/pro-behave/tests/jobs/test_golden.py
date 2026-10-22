"""Golden byte-shape tests for the serializers used by the job service.

These guard the exact on-disk bytes of a job's record and exit, plus the
JSON key ordering of tool response payloads.
"""

import json

from pro_behave.jobs.adapters import (
    LocalJobResultStore,
    LocalJobResultStoreFactory,
)
from pro_behave.jobs.messages import JobExit, JobRecord, JobRequest
from pro_behave.jobs.service import JobService
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


def test_write_exit_byte_shape(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    store.write_exit(
        "jobshape", JobExit(finished_at="T1", returncode=0, ok=True)
    )
    path = tmp_path / "jobshape" / "exit.json"
    expected = (
        "{\n"
        '  "error": "",\n'
        '  "finished_at": "T1",\n'
        '  "killed": false,\n'
        '  "ok": true,\n'
        '  "returncode": 0\n'
        "}\n"
    )
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


def test_completed_with_summary_key_order(tmp_path):
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "done\n", encoding="utf-8"
    )
    report = job_file(tmp_path, job_id, "report.json")
    report.write_text(
        json.dumps(
            [
                {
                    "name": "feature",
                    "elements": [
                        {
                            "name": "scenario",
                            "steps": [
                                {
                                    "name": "step",
                                    "result": {"status": "passed"},
                                }
                            ],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    launcher = _launched(tmp_path, job_id)
    launcher.finish(job_id, returncode=0)

    payload = (
        _service(tmp_path, launcher)
        .wait_for_job(job_id, max_wait_seconds=5, poll_interval_seconds=0.01)
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "job_id",
        "state",
        "ok",
        "feature",
        "scenario",
        "release",
        "machine_type",
        "summary",
        "failures",
        "returncode",
        "error",
        "artifacts",
        "recent_output",
    ]
    assert payload["recent_output"] is None


def test_completed_fallback_key_order(tmp_path):
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "boom\n", encoding="utf-8"
    )
    launcher = _launched(tmp_path, job_id)
    launcher.finish(job_id, returncode=2)

    payload = (
        _service(tmp_path, launcher)
        .wait_for_job(job_id, max_wait_seconds=5, poll_interval_seconds=0.01)
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "job_id",
        "state",
        "ok",
        "feature",
        "scenario",
        "release",
        "machine_type",
        "summary",
        "failures",
        "returncode",
        "error",
        "artifacts",
        "recent_output",
    ]


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
