import json
from pathlib import Path

import behave_mcp.server as server_module
from behave_mcp.server import (
    ACTIVE_JOBS,
    get_scenario_logs,
    list_features,
    start_behave_scenario,
    wait_for_scenario_completion,
)


def test_list_features_returns_feature_files(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))

    result = json.loads(list_features())

    assert "features" in result
    assert "features/cli/attach.feature" in result["features"]


def test_start_behave_scenario_rejects_unlisted_feature(monkeypatch):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    result = json.loads(
        start_behave_scenario(
            "features/cli/does-not-exist.feature",
            machine_types=["lxd-container"],
        )
    )

    assert result["ok"] is False
    assert "Feature is not listed by list_features" in result["error"]


def test_start_behave_scenario_accepts_normalized_listed_feature(
    monkeypatch, tmp_path
):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    class FakePopen:
        def __init__(self, command, cwd, env, stdout, stderr, text):
            self.returncode = None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(server_module.subprocess, "Popen", FakePopen)

    result = json.loads(
        start_behave_scenario(
            "features/cli/../cli/attach.feature",
            machine_types=["lxd-container"],
        )
    )

    assert result["ok"] is True


def test_start_behave_scenario_builds_command(monkeypatch, tmp_path):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )
    monkeypatch.setenv("UACLIENT_BEHAVE_CONTRACT_TOKEN", "token")

    class FakePopen:
        def __init__(self, command, cwd, env, stdout, stderr, text):
            calls["command"] = command
            calls["cwd"] = cwd
            calls["env"] = env
            calls["stdout"] = stdout
            calls["stderr"] = stderr
            calls["text"] = text
            self.returncode = None

        def poll(self):
            return self.returncode

    calls = {}

    monkeypatch.setattr(server_module.subprocess, "Popen", FakePopen)

    result = json.loads(
        start_behave_scenario(
            "features/cli/attach.feature",
            machine_types=["lxd-container"],
            scenario_name="attach",
            releases=["resolute"],
        )
    )

    assert result["ok"] is True
    assert "job_id" in result
    assert calls["command"][:5] == [
        "tox",
        "-e",
        "behave",
        "--",
        "features/cli/attach.feature",
    ]
    assert "--name" in calls["command"]
    assert "-f" in calls["command"]
    assert "json" in calls["command"]
    assert calls["cwd"] == str(Path(__file__).resolve().parents[3])
    assert calls["env"]["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "token"


def test_start_behave_scenario_requires_machine_types(monkeypatch):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    result = json.loads(
        start_behave_scenario("features/cli/attach.feature", [])
    )

    assert result["ok"] is False
    assert "machine_types is required" in result["error"]


def test_start_behave_scenario_rejects_unsupported_machine_type(monkeypatch):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    result = json.loads(
        start_behave_scenario(
            "features/cli/attach.feature", machine_types=["azure.generic"]
        )
    )

    assert result["ok"] is False
    assert "Unsupported machine_types" in result["error"]


def test_wait_for_scenario_completion_running_to_completed(
    monkeypatch, tmp_path
):
    ACTIVE_JOBS.clear()
    job_id = "job12345"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    report = tmp_path / f"{job_id}_report.json"
    stdout_log.write_text("line1\nline2\n", encoding="utf-8")

    class FakeLogHandle:
        closed = False

        def close(self):
            self.closed = True

    class FakePopen:
        def __init__(self):
            self._returncode = None

        def poll(self):
            return self._returncode

    proc = FakePopen()
    ACTIVE_JOBS[job_id] = {
        "process": proc,
        "json_report": report,
        "stdout_log": stdout_log,
        "log_file_handle": FakeLogHandle(),
    }

    def fake_sleep(seconds):
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
                                        "name": "a step",
                                        "result": {
                                            "status": "failed",
                                            "error_message": "boom",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        proc._returncode = 1

    monkeypatch.setattr(server_module.time, "sleep", fake_sleep)

    completed = json.loads(
        wait_for_scenario_completion(
            job_id, max_wait_seconds=60, poll_interval_seconds=0.01
        )
    )
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"]["steps"]["failed"] == 1
    assert completed["failures"][0]["step"] == "a step"


def test_wait_for_scenario_completion_missing_report_fallback(tmp_path):
    ACTIVE_JOBS.clear()
    job_id = "job54321"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("setup failed\n", encoding="utf-8")

    class FakeLogHandle:
        closed = False

        def close(self):
            self.closed = True

    class FakePopen:
        def poll(self):
            return 2

    ACTIVE_JOBS[job_id] = {
        "process": FakePopen(),
        "json_report": tmp_path / "missing.json",
        "stdout_log": stdout_log,
        "log_file_handle": FakeLogHandle(),
    }

    completed = json.loads(wait_for_scenario_completion(job_id))
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"] is None
    assert "setup failed" in completed["recent_output"]


def test_wait_for_scenario_completion_timeout(monkeypatch, tmp_path):
    ACTIVE_JOBS.clear()
    job_id = "jobtimeout"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("still running\n", encoding="utf-8")

    class FakePopen:
        def poll(self):
            return None

    ACTIVE_JOBS[job_id] = {
        "process": FakePopen(),
        "json_report": tmp_path / "missing.json",
        "stdout_log": stdout_log,
        "log_file_handle": None,
    }

    monotonic_values = iter([0.0, 0.1, 0.6, 1.1])

    def fake_monotonic():
        return next(monotonic_values)

    monkeypatch.setattr(server_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(server_module.time, "sleep", lambda seconds: None)

    timeout = json.loads(
        wait_for_scenario_completion(
            job_id, max_wait_seconds=1, poll_interval_seconds=0.01
        )
    )
    assert timeout["ok"] is False
    assert timeout["status"] == "timeout"
    assert timeout["last_status"] == "running"
    assert "still running" in timeout["recent_output"]


def test_get_scenario_logs_returns_tail(tmp_path):
    ACTIVE_JOBS.clear()
    job_id = "jobtail"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("l1\nl2\nl3\n", encoding="utf-8")

    ACTIVE_JOBS[job_id] = {
        "process": None,
        "json_report": tmp_path / "none.json",
        "stdout_log": stdout_log,
        "log_file_handle": None,
    }

    result = json.loads(get_scenario_logs(job_id, lines=2))
    assert result["ok"] is True
    assert result["lines"] == 2
    assert result["output"] == "l2\nl3"


def test_main_uses_stdio_transport_by_default(monkeypatch):
    import behave_mcp.server as server_module

    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    captured = {}

    def fake_run(transport, mount_path=None):
        captured["transport"] = transport
        captured["mount_path"] = mount_path

    monkeypatch.setattr(server_module.mcp, "run", fake_run)

    server_module.main()

    assert captured["transport"] == "stdio"
