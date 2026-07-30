import json
import os
from pathlib import Path

import behave_mcp.server as server_module
import pytest
from behave_mcp.server import ACTIVE_JOBS, mcp
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.fixture(autouse=True)
def clear_jobs():
    ACTIVE_JOBS.clear()
    yield
    ACTIVE_JOBS.clear()


def _result_json(result):
    assert result.isError is False
    for block in result.content:
        if hasattr(block, "text"):
            return json.loads(block.text)
    raise AssertionError("Expected text content block in tool result")


def _make_fake_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "fake-repo"
    (repo_root / "features" / "cli").mkdir(parents=True)
    (repo_root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    (repo_root / "features" / "cli" / "sample.feature").write_text(
        "Feature: sample\n", encoding="utf-8"
    )
    return repo_root


class _FakeProcess:
    def __init__(self, report_path=None):
        self._report_path = report_path
        self._poll_count = 0
        self.returncode = None

    def poll(self):
        self._poll_count += 1
        if (
            self._report_path is not None
            and self._poll_count == 2
            and self.returncode is None
        ):
            self._report_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "attach feature",
                            "elements": [
                                {
                                    "name": "attach scenario",
                                    "steps": [
                                        {
                                            "name": "do attach",
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
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert "list_features" in tools
    assert "start_behave_scenario" in tools
    assert "wait_for_scenario_completion" in tools
    assert "get_scenario_logs" in tools
    assert "get_scenario_artifacts" in tools
    assert tools["start_behave_scenario"].description


@pytest.mark.asyncio
async def test_mcp_list_features_returns_json(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_features", {})

    payload = _result_json(result)
    assert "features" in payload
    assert "features/cli/attach.feature" in payload["features"]


@pytest.mark.asyncio
async def test_mcp_list_features_uses_repo_root_override(tmp_path):
    fake_repo = _make_fake_repo(tmp_path)

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "list_features", {"repo_root": str(fake_repo)}
        )

    payload = _result_json(result)
    assert payload["ok"] is True
    assert payload["repo_root"] == str(fake_repo)
    assert payload["features"] == ["features/cli/sample.feature"]


@pytest.mark.asyncio
async def test_mcp_list_features_rejects_invalid_repo_root(tmp_path):
    invalid_repo = tmp_path / "invalid-root"
    invalid_repo.mkdir()

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "list_features", {"repo_root": str(invalid_repo)}
        )

    payload = _result_json(result)
    assert payload["ok"] is False
    assert "Invalid repo_root" in payload["error"]


@pytest.mark.asyncio
async def test_mcp_start_wait_and_log_flow(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path))

    def fake_popen(command, cwd, env, stdout, stderr, text):
        report_path = Path(command[command.index("-o") + 1])
        stdout.write("line1\nline2\nline3\n")
        stdout.flush()
        proc = _FakeProcess(report_path=report_path)
        return proc

    monkeypatch.setattr(server_module.subprocess, "Popen", fake_popen)

    async with create_connected_server_and_client_session(mcp) as client:
        start_result = await client.call_tool(
            "start_behave_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": ["lxd-container"],
                "releases": ["noble"],
            },
        )
        start_payload = _result_json(start_result)
        assert start_payload["ok"] is True
        assert start_payload["status"] == "started"
        assert "artifacts" in start_payload
        job_id = start_payload["job_id"]

        completed_result = await client.call_tool(
            "wait_for_scenario_completion",
            {
                "job_id": job_id,
                "max_wait_seconds": 5,
                "poll_interval_seconds": 0.01,
            },
        )
        completed_payload = _result_json(completed_result)
        assert completed_payload["status"] == "completed"
        assert completed_payload["ok"] is True
        assert completed_payload["summary"]["steps"]["passed"] == 1
        assert "artifacts" in completed_payload

        logs_result = await client.call_tool(
            "get_scenario_logs", {"job_id": job_id, "lines": 2}
        )
        logs_payload = _result_json(logs_result)
        assert logs_payload["ok"] is True
        assert logs_payload["output"] == "line2\nline3"
        assert logs_payload["output_lines"] == ["line2", "line3"]

        artifacts_result = await client.call_tool(
            "get_scenario_artifacts", {"job_id": job_id}
        )
        artifacts_payload = _result_json(artifacts_result)
        assert artifacts_payload["ok"] is True
        assert artifacts_payload["exists"]["stdout_log"] is True


@pytest.mark.e2e
@pytest.mark.long_running
@pytest.mark.asyncio
async def test_mcp_e2e_long_running_attach_flow(monkeypatch):
    contract_token = os.environ.get("UACLIENT_BEHAVE_CONTRACT_TOKEN")
    if not contract_token:
        pytest.skip(
            "UACLIENT_BEHAVE_CONTRACT_TOKEN is required for real attach e2e"
        )

    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("UACLIENT_BEHAVE_CONTRACT_TOKEN", contract_token)

    async with create_connected_server_and_client_session(mcp) as client:
        start_result = await client.call_tool(
            "start_behave_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": ["lxd-container"],
                "releases": ["noble"],
            },
        )
        start_payload = _result_json(start_result)
        assert start_payload["ok"] is True
        assert start_payload["status"] == "started"
        job_id = start_payload["job_id"]

        completed_payload = None
        try:
            status_result = await client.call_tool(
                "wait_for_scenario_completion",
                {
                    "job_id": job_id,
                    "max_wait_seconds": 1800,
                    "poll_interval_seconds": 5,
                },
            )
            completed_payload = _result_json(status_result)
        finally:
            job = ACTIVE_JOBS.get(job_id)
            if job and job.get("process") is not None:
                process = job["process"]
                if process.poll() is None:
                    process.terminate()

        assert completed_payload is not None
        assert completed_payload["status"] == "completed"
        assert isinstance(completed_payload.get("ok"), bool)
        assert "returncode" in completed_payload
        assert completed_payload["summary"] is not None
        assert completed_payload["summary"]["scenarios"]["total"] > 0
        assert completed_payload["summary"]["steps"]["total"] > 0


@pytest.mark.asyncio
async def test_mcp_start_requires_machine_types():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_behave_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": [],
            },
        )

    payload = _result_json(result)
    assert payload["ok"] is False
    assert "machine_types is required" in payload["error"]


@pytest.mark.asyncio
async def test_mcp_start_rejects_unlisted_feature():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_behave_scenario",
            {
                "feature_file": "features/cli/does-not-exist.feature",
                "machine_types": ["lxd-container"],
            },
        )

    payload = _result_json(result)
    assert payload["ok"] is False
    assert "Feature is not listed by list_features" in payload["error"]


@pytest.mark.asyncio
async def test_mcp_start_rejects_cloud_machine_types():
    """
    Avoid accidental calls to cloud providers for now.

    In the future, this might change.
    """

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_behave_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": ["azure.generic"],
            },
        )

    payload = _result_json(result)
    assert payload["ok"] is False
    assert "Cloud machine_types are disabled by default" in payload["error"]
