import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import pro_behave.mcp.server as server_module
from pro_behave.jobs.adapters import LocalWorkspace
from pro_behave.mcp.server import mcp
from tests.conftest import (
    UNIT_SCENARIO,
    FakeJobLauncher,
    make_repo_with_feature,
    make_service,
    result_error_text,
    result_json,
)

pytestmark = pytest.mark.integration

# The real ubuntu-pro-client repo this package lives in.
# Used by tests that need to parse actual features/*.feature files.
_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.asyncio
async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    # One name per command, shared with the CLI and the service.
    assert set(tools) == {
        "list_features",
        "describe_feature",
        "list_dimensions",
        "find_scenarios",
        "run_job",
        "list_jobs",
        "job_results",
        "wait_for_job",
        "job_errors",
        "job_logs",
        "job_artifacts",
        "kill_job",
        "create_campaign",
        "list_campaigns",
        "campaign_status",
        "unit_history",
        "control_campaign",
        "await_campaign_events",
        "retry_units",
        "export_campaign",
    }
    assert all(tool.description for tool in tools.values())


@pytest.mark.asyncio
async def test_every_tool_call_ends_with_a_reconcile(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))
    reconciles = []
    monkeypatch.setattr(
        server_module._service, "reconcile", lambda: reconciles.append(1)
    )

    async with create_connected_server_and_client_session(mcp) as client:
        # A tool that touches no job: the queue still moves.
        await client.call_tool("list_dimensions", {})

    assert reconciles == [1]


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
            {"feature": "features/cli/attach.feature"},
        )

    payload = result_json(result)
    assert payload["feature"] == "features/cli/attach.feature"
    assert payload["scenarios"]


@pytest.mark.asyncio
async def test_mcp_list_dimensions_returns_values(monkeypatch):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_dimensions", {})

    payload = result_json(result)
    assert payload["releases"]
    assert payload["machine_types"]


# A scenario of the real features/cli/attach.feature with a jammy
# lxd-container row.
REAL_UNIT_SCENARIO = "Attach command with json output"


@pytest.mark.asyncio
async def test_mcp_run_job_wait_and_log_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(_REPO_ROOT))
    monkeypatch.setenv("PRO_BEHAVE_STATE_DIR", str(tmp_path))

    # Real wiring but for the launcher: each job ends as soon as it starts,
    # with a log and a passing result, as a real job process would leave them.
    launcher = FakeJobLauncher(
        tmp_path / "jobs",
        finish_on_launch=dict(log="line1\nline2\nline3\n"),
    )
    monkeypatch.setattr(
        server_module,
        "_service",
        make_service(
            LocalWorkspace(),
            launcher=launcher,
            monotonic=time.monotonic,
            sleep=time.sleep,
            new_job_id=lambda: "job-flow",
        ),
    )

    async with create_connected_server_and_client_session(mcp) as client:
        start_result = await client.call_tool(
            "run_job",
            {
                "feature": "features/cli/attach.feature",
                "scenario": REAL_UNIT_SCENARIO,
                "release": "jammy",
                "machine_type": "lxd-container",
            },
        )
        start_payload = result_json(start_result)
        # The fake job ends the moment it starts.
        assert start_payload["state"] == "finished"
        assert "artifacts" in start_payload
        job_id = start_payload["job_id"]

        completed_result = await client.call_tool(
            "wait_for_job",
            {
                "job_id": job_id,
                "max_wait_seconds": 5,
                "poll_interval_seconds": 0.01,
            },
        )
        completed_payload = result_json(completed_result)
        assert completed_payload["state"] == "finished"
        assert completed_payload["outcome"] == "passed"
        assert "artifacts" in completed_payload

        logs_result = await client.call_tool(
            "job_logs", {"job_id": job_id, "lines": 2}
        )
        logs_payload = result_json(logs_result)
        assert logs_payload["text"] == "2: line2\n3: line3"

        artifacts_result = await client.call_tool(
            "job_artifacts", {"job_id": job_id}
        )
        artifacts_payload = result_json(artifacts_result)
        assert artifacts_payload["exists"]["stdout_log"] is True

        jobs_result = await client.call_tool("list_jobs", {})
        jobs_payload = result_json(jobs_result)
        assert job_id in {job["job_id"] for job in jobs_payload["jobs"]}

        results_result = await client.call_tool(
            "job_results", {"job_ids": [job_id]}
        )
        results_payload = result_json(results_result)
        assert results_payload["total"] == 1
        (result,) = results_payload["results"]
        assert result["job_id"] == job_id
        assert result["outcome"] == "passed"
        assert result["failures"] == []
        # One result for one job, however it is asked for.
        for key in ("state", "outcome", "instances", "failures", "release"):
            assert result[key] == completed_payload[key]


@pytest.mark.asyncio
async def test_mcp_run_job_requires_a_whole_unit():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "run_job",
            {
                "feature": "features/cli/attach.feature",
                "scenario": UNIT_SCENARIO,
                "release": "jammy",
            },
        )

    assert "machine_type" in result_error_text(result)


@pytest.mark.asyncio
async def test_mcp_run_job_rejects_unlisted_feature():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "run_job",
            {
                "feature": "features/cli/does-not-exist.feature",
                "scenario": UNIT_SCENARIO,
                "release": "jammy",
                "machine_type": "lxd-container",
            },
        )

    error_text = result_error_text(result)
    assert "Feature is not listed by list_features" in error_text


@pytest.mark.asyncio
async def test_mcp_run_job_rejects_cloud_machine_types():
    """
    Avoid accidental calls to cloud providers for now.

    In the future, this might change.
    """

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "run_job",
            {
                "feature": "features/cli/attach.feature",
                "scenario": UNIT_SCENARIO,
                "release": "jammy",
                "machine_type": "azure.generic",
            },
        )

    error_text = result_error_text(result)
    assert "max_jobs.cloud" in error_text


def test_main_serves_streamable_http(monkeypatch):
    captured = {}

    def fake_run(transport, mount_path=None):
        captured["transport"] = transport
        captured["mount_path"] = mount_path

    monkeypatch.setattr(server_module.mcp, "run", fake_run)

    server_module.main()

    assert captured["transport"] == "streamable-http"
