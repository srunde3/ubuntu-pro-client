import json
from pathlib import Path

from behave_mcp.server import (
    ACTIVE_JOBS,
    ALLOWED_FEATURES,
    check_scenario_status,
    get_scenario_logs,
    list_features,
    start_behave_scenario,
)


def test_list_features_returns_feature_files(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))

    result = json.loads(list_features())

    assert "features" in result
    assert "features/cli/attach.feature" in result["features"]


def test_start_behave_scenario_rejects_disallowed_feature(monkeypatch):
    ACTIVE_JOBS.clear()
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    result = json.loads(
        start_behave_scenario("features/cli/not-allowed.feature")
    )

    assert result["ok"] is False
    assert "Feature not allowed" in result["error"]


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

    import behave_mcp.server as server_module

    monkeypatch.setattr(server_module.subprocess, "Popen", FakePopen)

    result = json.loads(
        start_behave_scenario(
            next(iter(ALLOWED_FEATURES)),
            scenario_name="attach",
            releases=["resolute"],
            machine_types=["lxd-container"],
        )
    )

    assert result["ok"] is True
    assert "job_id" in result
    assert calls["command"][:5] == [
        "tox",
        "-e",
        "behave",
        "--",
        next(iter(ALLOWED_FEATURES)),
    ]
    assert "--name" in calls["command"]
    assert "-f" in calls["command"]
    assert "json" in calls["command"]
    assert calls["cwd"].endswith("ubuntu-pro-client")
    assert calls["env"]["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "token"


def test_check_scenario_status_running_and_completed(monkeypatch, tmp_path):
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

    running = json.loads(check_scenario_status(job_id))
    assert running["status"] == "running"
    assert "line2" in running["recent_output"]

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

    completed = json.loads(check_scenario_status(job_id))
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"]["steps"]["failed"] == 1
    assert completed["failures"][0]["step"] == "a step"


def test_check_scenario_status_missing_report_fallback(tmp_path):
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

    completed = json.loads(check_scenario_status(job_id))
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"] is None
    assert "setup failed" in completed["recent_output"]


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
