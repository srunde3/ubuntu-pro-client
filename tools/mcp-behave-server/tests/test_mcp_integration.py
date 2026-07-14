import asyncio
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


class _FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.mark.asyncio
async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert "list_features" in tools
    assert "start_behave_scenario" in tools
    assert "check_scenario_status" in tools
    assert "get_scenario_logs" in tools
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
async def test_mcp_start_check_and_log_flow(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path))

    proc = _FakeProcess()

    def fake_popen(command, cwd, env, stdout, stderr, text):
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
        job_id = start_payload["job_id"]

        running_result = await client.call_tool(
            "check_scenario_status", {"job_id": job_id}
        )
        running_payload = _result_json(running_result)
        assert running_payload["status"] == "running"

        # Simulate process completion and a report file emitted by behave.
        report_path = tmp_path / f"{job_id}_report.json"
        report_path.write_text(
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
        log_path = tmp_path / f"{job_id}_stdout.log"
        log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
        proc.returncode = 0

        completed_result = await client.call_tool(
            "check_scenario_status", {"job_id": job_id}
        )
        completed_payload = _result_json(completed_result)
        assert completed_payload["status"] == "completed"
        assert completed_payload["ok"] is True
        assert completed_payload["summary"]["steps"]["passed"] == 1

        logs_result = await client.call_tool(
            "get_scenario_logs", {"job_id": job_id, "lines": 2}
        )
        logs_payload = _result_json(logs_result)
        assert logs_payload["ok"] is True
        assert logs_payload["output"] == "line2\nline3"


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
            # Long-running poll loop: allow up to 30 minutes.
            for _ in range(360):
                status_result = await client.call_tool(
                    "check_scenario_status", {"job_id": job_id}
                )
                status_payload = _result_json(status_result)

                if status_payload["status"] == "completed":
                    completed_payload = status_payload
                    break

                assert status_payload["status"] == "running"
                await asyncio.sleep(5)
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
    assert "Unsupported machine_types" in payload["error"]
