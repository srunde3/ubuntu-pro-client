from pathlib import Path

import pytest
from conftest import (
    FakeProcess,
    make_repo_with_feature,
    result_error_text,
    result_json,
)
from mcp.shared.memory import create_connected_server_and_client_session

import behave_mcp.adapters as adapters_module
import behave_mcp.server as server_module
from behave_mcp.server import mcp

# The real ubuntu-pro-client repo this package lives in.
# Used by tests that need to parse actual features/*.feature files.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert "list_features" in tools
    assert "describe_feature" in tools
    assert "list_dimensions" in tools
    assert "find_scenarios" in tools
    assert "start_scenario" in tools
    assert "list_scenario_jobs" in tools
    assert "summarize_scenario_results" in tools
    assert "wait_for_scenario_completion" in tools
    assert "get_scenario_logs" in tools
    assert "get_scenario_artifacts" in tools
    assert tools["start_scenario"].description


@pytest.mark.asyncio
async def test_mcp_list_features_returns_json(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_features", {})

    payload = result_json(result)
    assert "features" in payload
    paths = [feature["path"] for feature in payload["features"]]
    assert "features/cli/attach.feature" in paths


@pytest.mark.asyncio
async def test_mcp_list_features_uses_repo_root_override(tmp_path):
    fake_repo = make_repo_with_feature(tmp_path)

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "list_features", {"repo_root": str(fake_repo)}
        )

    payload = result_json(result)
    assert payload["repo_root"] == str(fake_repo)
    assert [feature["path"] for feature in payload["features"]] == [
        "features/cli/sample.feature"
    ]


@pytest.mark.asyncio
async def test_mcp_list_features_rejects_invalid_repo_root(tmp_path):
    invalid_repo = tmp_path / "invalid-root"
    invalid_repo.mkdir()

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "list_features", {"repo_root": str(invalid_repo)}
        )

    error_text = result_error_text(result)
    assert "Invalid repo_root" in error_text


@pytest.mark.asyncio
async def test_mcp_describe_feature_returns_detail(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "describe_feature",
            {"feature_file": "features/cli/attach.feature"},
        )

    payload = result_json(result)
    assert payload["feature_file"] == "features/cli/attach.feature"
    assert payload["scenarios"]


@pytest.mark.asyncio
async def test_mcp_list_dimensions_returns_values(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_dimensions", {})

    payload = result_json(result)
    assert payload["releases"]
    assert payload["machine_types"]


@pytest.mark.asyncio
async def test_mcp_find_scenarios_matches_by_text(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("find_scenarios", {"text": "attach"})

    payload = result_json(result)
    assert payload["matches"]
    assert all(
        "attach" in match["scenario_name"].lower()
        for match in payload["matches"]
    )


@pytest.mark.asyncio
async def test_mcp_start_wait_and_log_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path))

    def fake_popen(command, cwd, env, stdout, stderr, text):
        report_path = Path(command[command.index("-o") + 1])
        stdout.write("line1\nline2\nline3\n")
        stdout.flush()
        proc = FakeProcess(report_path=report_path)
        return proc

    monkeypatch.setattr(adapters_module.subprocess, "Popen", fake_popen)

    async with create_connected_server_and_client_session(mcp) as client:
        start_result = await client.call_tool(
            "start_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": ["lxd-container"],
                "releases": ["noble"],
            },
        )
        start_payload = result_json(start_result)
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
        completed_payload = result_json(completed_result)
        assert completed_payload["status"] == "completed"
        assert completed_payload["ok"] is True
        assert completed_payload["summary"]["steps"]["passed"] == 1
        assert "artifacts" in completed_payload

        logs_result = await client.call_tool(
            "get_scenario_logs", {"job_id": job_id, "lines": 2}
        )
        logs_payload = result_json(logs_result)
        assert logs_payload["output"] == "line2\nline3"
        assert logs_payload["output_lines"] == ["line2", "line3"]

        artifacts_result = await client.call_tool(
            "get_scenario_artifacts", {"job_id": job_id}
        )
        artifacts_payload = result_json(artifacts_result)
        assert artifacts_payload["exists"]["stdout_log"] is True

        jobs_result = await client.call_tool("list_scenario_jobs", {})
        jobs_payload = result_json(jobs_result)
        assert job_id in {job["job_id"] for job in jobs_payload["jobs"]}

        summary_result = await client.call_tool(
            "summarize_scenario_results", {"job_ids": [job_id]}
        )
        summary_payload = result_json(summary_result)
        assert summary_payload["matched_job_ids"] == [job_id]
        assert summary_payload["job_counts"]["completed_passed"] == 1
        assert summary_payload["job_counts"]["total"] == 1
        total_passed = sum(
            group["passed"] for group in summary_payload["by_release"]
        )
        assert total_passed == 1
        assert summary_payload["failures"] == []


@pytest.mark.asyncio
async def test_mcp_start_requires_machine_types():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": [],
            },
        )

    error_text = result_error_text(result)
    assert "machine_types is required" in error_text


@pytest.mark.asyncio
async def test_mcp_start_rejects_unlisted_feature():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_scenario",
            {
                "feature_file": "features/cli/does-not-exist.feature",
                "machine_types": ["lxd-container"],
            },
        )

    error_text = result_error_text(result)
    assert "Feature is not listed by list_features" in error_text


@pytest.mark.asyncio
async def test_mcp_start_rejects_cloud_machine_types():
    """
    Avoid accidental calls to cloud providers for now.

    In the future, this might change.
    """

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_scenario",
            {
                "feature_file": "features/cli/attach.feature",
                "machine_types": ["azure.generic"],
            },
        )

    error_text = result_error_text(result)
    assert "Cloud machine_types are disabled by default" in error_text


def test_main_uses_stdio_transport_by_default(monkeypatch):
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    captured = {}

    def fake_run(transport, mount_path=None):
        captured["transport"] = transport
        captured["mount_path"] = mount_path

    monkeypatch.setattr(server_module.mcp, "run", fake_run)

    server_module.main()

    assert captured["transport"] == "stdio"
