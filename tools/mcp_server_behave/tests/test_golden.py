"""Golden byte-shape tests for the serializers used by the behave MCP server.

These guard the exact on-disk output produced for job metadata and the
per-log-dir index, plus the JSON key ordering of tool response payloads.
"""

import json

from conftest import FakeWorkspace

from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalArtifactStore,
    LocalFeatureCatalog,
    LocalFeatureFileReader,
)
from behave_mcp.config import Settings
from behave_mcp.ports import Job
from behave_mcp.service import BehaveService


def test_write_metadata_byte_shape(tmp_path):
    store = LocalArtifactStore()
    path = tmp_path / "m.json"
    store.write_metadata(path, {"b": 1, "a": 2})
    assert path.read_text(encoding="utf-8") == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_append_index_event_byte_shape(tmp_path):
    store = LocalArtifactStore()
    store.append_index_event(tmp_path, {"b": 1, "a": 2})
    store.append_index_event(tmp_path, {"event": "completed", "job_id": "x"})
    content = (tmp_path / "index.jsonl").read_text(encoding="utf-8")
    assert content == (
        '{"a": 2, "b": 1}\n' '{"event": "completed", "job_id": "x"}\n'
    )


_SETTINGS = Settings(
    allow_cloud_machine_types=False,
    max_parallel_jobs=1,
    transport="stdio",
    host="127.0.0.1",
    port=8000,
)


def _service(tmp_path, registry) -> BehaveService:
    return BehaveService(
        workspace=FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        settings=_SETTINGS,
        feature_reader=LocalFeatureFileReader(),
        feature_catalog=LocalFeatureCatalog(),
        artifact_store=LocalArtifactStore(),
        registry=registry,
        launcher=None,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
        now_utc=lambda: "T0",
        new_job_id=lambda: "job0001",
    )


class _Handle:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def close(self):
        pass

    def terminate(self):
        pass


def _register(registry, tmp_path, job_id, handle, report=None):
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            stdout_log=tmp_path / f"{job_id}_stdout.log",
            json_report=(
                report
                if report is not None
                else tmp_path / f"{job_id}_report.json"
            ),
            metadata=tmp_path / f"{job_id}_meta.json",
        ),
    )


def test_completed_with_summary_key_order(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job0001"
    (tmp_path / f"{job_id}_stdout.log").write_text("done\n", encoding="utf-8")
    report = tmp_path / f"{job_id}_report.json"
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
    _register(registry, tmp_path, job_id, _Handle(0), report=report)

    payload = (
        _service(tmp_path, registry)
        .wait_for_completion(
            job_id, max_wait_seconds=5, poll_interval_seconds=0.01
        )
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "status",
        "ok",
        "job_id",
        "returncode",
        "artifacts",
        "summary",
        "failures",
        "recent_output",
    ]
    assert payload["recent_output"] is None


def test_completed_fallback_key_order(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job0001"
    (tmp_path / f"{job_id}_stdout.log").write_text("boom\n", encoding="utf-8")
    _register(registry, tmp_path, job_id, _Handle(2))

    payload = (
        _service(tmp_path, registry)
        .wait_for_completion(
            job_id, max_wait_seconds=5, poll_interval_seconds=0.01
        )
        .model_dump(mode="json")
    )

    assert list(payload.keys()) == [
        "status",
        "ok",
        "job_id",
        "returncode",
        "artifacts",
        "summary",
        "failures",
        "recent_output",
    ]


def test_timeout_key_order(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job0001"
    (tmp_path / f"{job_id}_stdout.log").write_text(
        "running\n", encoding="utf-8"
    )
    _register(registry, tmp_path, job_id, _Handle(None))

    values = iter([0.0, 1.1])
    service = BehaveService(
        workspace=FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        settings=_SETTINGS,
        feature_reader=LocalFeatureFileReader(),
        feature_catalog=LocalFeatureCatalog(),
        artifact_store=LocalArtifactStore(),
        registry=registry,
        launcher=None,
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
