"""Golden byte-shape tests for the serializers used by the behave MCP server.

These guard the exact on-disk output produced for job metadata and the
per-log-dir index, plus the JSON key ordering of tool response payloads.
"""

import json

from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
)
from behave_mcp.config import Settings
from behave_mcp.domain.pools import DEFAULT_POOL_LIMITS
from behave_mcp.messages import JobExit, JobRecord
from behave_mcp.ports import Job
from behave_mcp.service import BehaveService
from tests.conftest import (
    FakeLauncher,
    FakeProcessHandle,
    FakeWorkspace,
    job_file,
)


def test_write_record_byte_shape(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    store.write_record("jobshape", JobRecord(job_id="jobshape"))
    path = tmp_path / "jobshape" / "job.json"
    expected = (
        "{\n"
        '  "artifacts": null,\n'
        '  "command": [],\n'
        '  "feature_file": "",\n'
        '  "install_from": "",\n'
        '  "job_id": "jobshape",\n'
        '  "machine_type": "",\n'
        '  "pid": null,\n'
        '  "release": "",\n'
        '  "repo_root": "",\n'
        '  "repo_state": null,\n'
        '  "scenario_name": "",\n'
        '  "started_at": null\n'
        "}\n"
    )
    assert path.read_text(encoding="utf-8") == expected


def test_write_exit_byte_shape(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    store.write_exit(
        "jobshape", JobExit(completed_at="T1", returncode=0, ok=True)
    )
    path = tmp_path / "jobshape" / "status.json"
    expected = (
        "{\n"
        '  "completed_at": "T1",\n'
        '  "ok": true,\n'
        '  "returncode": 0\n'
        "}\n"
    )
    assert path.read_text(encoding="utf-8") == expected


_SETTINGS = Settings(
    pool_limits=DEFAULT_POOL_LIMITS,
    campaign_poll_timeout=60,
    host="127.0.0.1",
    port=8000,
)


def _service(tmp_path, registry) -> BehaveService:
    return BehaveService(
        workspace=FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        settings=_SETTINGS,
        feature_reader=LocalFeatureFileReader(),
        results=LocalJobResultStoreFactory(),
        registry=registry,
        launcher=FakeLauncher(),
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
        now_utc=lambda: "T0",
        new_job_id=lambda: "job0001",
    )


def _register(registry, tmp_path, job_id, handle):
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            log_dir=tmp_path,
        ),
    )


def test_completed_with_summary_key_order(tmp_path):
    registry = InMemoryJobRegistry()
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
    _register(registry, tmp_path, job_id, FakeProcessHandle(0))

    payload = (
        _service(tmp_path, registry)
        .wait_for_completion(
            job_id, max_wait_seconds=5, poll_interval_seconds=0.01
        )
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "job_id",
        "status",
        "ok",
        "feature_file",
        "scenario_name",
        "release",
        "machine_type",
        "summary",
        "failures",
        "returncode",
        "artifacts",
        "recent_output",
    ]
    assert payload["recent_output"] is None


def test_completed_fallback_key_order(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "boom\n", encoding="utf-8"
    )
    _register(registry, tmp_path, job_id, FakeProcessHandle(2))

    payload = (
        _service(tmp_path, registry)
        .wait_for_completion(
            job_id, max_wait_seconds=5, poll_interval_seconds=0.01
        )
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "job_id",
        "status",
        "ok",
        "feature_file",
        "scenario_name",
        "release",
        "machine_type",
        "summary",
        "failures",
        "returncode",
        "artifacts",
        "recent_output",
    ]


def test_timeout_key_order(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job0001"
    (job_file(tmp_path, job_id, "stdout.log")).write_text(
        "running\n", encoding="utf-8"
    )
    _register(registry, tmp_path, job_id, FakeProcessHandle(None))

    values = iter([0.0, 1.1])
    service = BehaveService(
        workspace=FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        settings=_SETTINGS,
        feature_reader=LocalFeatureFileReader(),
        results=LocalJobResultStoreFactory(),
        registry=registry,
        launcher=FakeLauncher(),
        monotonic=lambda: next(values),
        sleep=lambda seconds: None,
        now_utc=lambda: "T0",
        new_job_id=lambda: job_id,
    )

    payload = service.wait_for_completion(
        job_id, max_wait_seconds=1, poll_interval_seconds=0.01
    ).model_dump(mode="json")

    assert list(payload.keys()) == [
        "status",
        "ok",
        "job_id",
        "max_wait_seconds",
        "poll_interval_seconds",
        "last_status",
        "recent_output",
        "artifacts",
    ]
