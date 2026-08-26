"""Real end-to-end MCP tests: actual subprocess/LXD infrastructure only.
"""

import os
from pathlib import Path

import pytest
from conftest import result_json
from mcp.shared.memory import create_connected_server_and_client_session

from behave_mcp.server import mcp, registry


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
        start_payload = result_json(start_result)
        assert start_payload["ok"] is True
        assert start_payload["status"] == "started"
        job_id = start_payload["job_id"]

        jobs_while_running = await client.call_tool("list_scenario_jobs", {})
        running_jobs = {
            job["job_id"]: job
            for job in result_json(jobs_while_running)["jobs"]
        }
        assert running_jobs[job_id]["status"] == "running"
        assert running_jobs[job_id]["feature_file"] == (
            "features/cli/attach.feature"
        )
        assert running_jobs[job_id]["machine_types"] == ["lxd-container"]

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
            completed_payload = result_json(status_result)
        finally:
            job = registry.get(job_id)
            if job and job.process_handle is not None:
                process = job.process_handle
                if process.poll() is None:
                    process.terminate()

        assert completed_payload is not None
        assert completed_payload["status"] == "completed"
        assert isinstance(completed_payload.get("ok"), bool)
        assert "returncode" in completed_payload
        assert completed_payload["summary"] is not None
        assert completed_payload["summary"]["scenarios"]["total"] > 0
        assert completed_payload["summary"]["steps"]["total"] > 0

        jobs_after_completion = await client.call_tool(
            "list_scenario_jobs", {}
        )
        finished_jobs = {
            job["job_id"]: job
            for job in result_json(jobs_after_completion)["jobs"]
        }
        assert finished_jobs[job_id]["status"] == "completed"
        assert finished_jobs[job_id]["ok"] == completed_payload["ok"]

        logs_result = await client.call_tool(
            "get_scenario_logs", {"job_id": job_id, "lines": 50}
        )
        logs_payload = result_json(logs_result)
        assert logs_payload["output"]
        assert logs_payload["output_lines"]

        artifacts_result = await client.call_tool(
            "get_scenario_artifacts", {"job_id": job_id}
        )
        artifacts_payload = result_json(artifacts_result)
        assert artifacts_payload["exists"]["stdout_log"] is True
        assert artifacts_payload["exists"]["json_report"] is True
        assert artifacts_payload["exists"]["metadata"] is True
        assert artifacts_payload["metadata"]["status"] == "completed"
        assert finished_jobs[job_id]["ok"] == completed_payload["ok"]
