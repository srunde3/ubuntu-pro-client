import json
from pathlib import Path

from behave_mcp.server import (
    ALLOWED_FEATURES,
    list_features,
    run_behave_scenario,
)


def test_list_features_returns_feature_files(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))

    result = json.loads(list_features())

    assert "features" in result
    assert "features/cli/attach.feature" in result["features"]


def test_run_behave_scenario_rejects_disallowed_feature(monkeypatch):
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )

    result = json.loads(
        run_behave_scenario("features/cli/not-allowed.feature")
    )

    assert result["ok"] is False
    assert "Feature not allowed" in result["error"]


def test_run_behave_scenario_builds_command(monkeypatch):
    monkeypatch.setenv(
        "UBUNTU_PRO_CLIENT_REPO", str(Path(__file__).resolve().parents[3])
    )
    monkeypatch.setenv("UACLIENT_BEHAVE_CONTRACT_TOKEN", "token")

    class FakeCompletedProcess:
        def __init__(self):
            self.returncode = 0
            self.stdout = "ok"
            self.stderr = ""

    calls = {}

    def fake_run(command, cwd, env, capture_output, text, timeout):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["timeout"] = timeout
        return FakeCompletedProcess()

    import behave_mcp.server as server_module

    monkeypatch.setattr(server_module.subprocess, "run", fake_run)

    result = json.loads(
        run_behave_scenario(
            next(iter(ALLOWED_FEATURES)),
            scenario_name="attach",
            releases=["resolute"],
            machine_types=["lxd-container"],
            timeout=900,
        )
    )

    assert result["ok"] is True
    assert calls["command"][:5] == [
        "tox",
        "-e",
        "behave",
        "--",
        next(iter(ALLOWED_FEATURES)),
    ]
    assert calls["cwd"].endswith("ubuntu-pro-client")
    assert calls["env"]["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "token"
    assert calls["timeout"] == 900
