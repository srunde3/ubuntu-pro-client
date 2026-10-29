"""Real end-to-end MCP tests: actual subprocess/LXD infrastructure only.
"""

import os
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import pro_e2e.mcp.server as server_module
from pro_e2e.mcp.server import mcp
from tests.conftest import result_json


@pytest.mark.e2e
@pytest.mark.long_running
@pytest.mark.asyncio
async def test_mcp_e2e_long_running_attach_flow(monkeypatch):
    contract_token = os.environ.get("UACLIENT_BEHAVE_CONTRACT_TOKEN")
    if not contract_token:
        pytest.skip(
            "UACLIENT_BEHAVE_CONTRACT_TOKEN is required for real attach e2e"
        )

    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    # Jobs take behave settings only from the checkout's config.
    server_module._service.set_config("behave.contract_token", contract_token)

    async with create_connected_server_and_client_session(mcp) as client:
        start_result = await client.call_tool(
            "run_job",
            {
                "feature": "features/cli/attach.feature",
                "scenario": "Attach command with json output",
                "release": "noble",
                "machine_type": "lxd-container",
            },
        )
        start_payload = result_json(start_result)
        assert start_payload["state"] == "running"
        job_id = start_payload["job_id"]

        jobs_while_running = await client.call_tool("list_jobs", {})
        running_jobs = {
            job["job_id"]: job
            for job in result_json(jobs_while_running)["jobs"]
        }
        assert running_jobs[job_id]["state"] == "running"
        assert running_jobs[job_id]["feature"] == (
            "features/cli/attach.feature"
        )
        assert running_jobs[job_id]["machine_type"] == "lxd-container"

        completed_payload = None
        try:
            status_result = await client.call_tool(
                "wait_for_job",
                {
                    "job_id": job_id,
                    "max_wait_seconds": 1800,
                    "poll_interval_seconds": 5,
                },
            )
            completed_payload = result_json(status_result)
        finally:
            # A job that outlives the test holds an LXD instance.
            await client.call_tool("kill_job", {"job_id": job_id})

        assert completed_payload is not None
        assert completed_payload["state"] == "finished"
        assert completed_payload["outcome"] in ("passed", "failed")
        assert "returncode" in completed_payload
        assert completed_payload["instances"]

        jobs_after_completion = await client.call_tool("list_jobs", {})
        finished_jobs = {
            job["job_id"]: job
            for job in result_json(jobs_after_completion)["jobs"]
        }
        assert finished_jobs[job_id]["state"] == "finished"
        assert finished_jobs[job_id]["outcome"] == completed_payload["outcome"]

        logs_result = await client.call_tool(
            "job_logs", {"job_id": job_id, "lines": 50}
        )
        logs_payload = result_json(logs_result)
        assert logs_payload["text"]
        assert logs_payload["total_lines"] > 0

        artifacts_result = await client.call_tool(
            "job_artifacts", {"job_id": job_id}
        )
        artifacts_payload = result_json(artifacts_result)
        assert artifacts_payload["exists"]["stdout_log"] is True
        assert artifacts_payload["exists"]["json_report"] is True
        assert artifacts_payload["exists"]["record"] is True
        assert artifacts_payload["exists"]["result"] is True
        assert (
            artifacts_payload["result"]["outcome"]
            == completed_payload["outcome"]
        )
        # repo_root here should be a real Pro client git checkout.
        repo = artifacts_payload["record"]["repo"]
        assert len(repo["commit"]) == 40
        assert repo["branch"]
        assert isinstance(repo["dirty"], bool)
        assert artifacts_payload["record"]["config"] == {
            "contract_token": "<set>"
        }
        assert finished_jobs[job_id]["outcome"] == completed_payload["outcome"]

        results = result_json(
            await client.call_tool("job_results", {"job_ids": [job_id]})
        )
        (result,) = results["results"]
        assert result["job_id"] == job_id
        assert result["state"] == "finished"
        assert result["outcome"] == completed_payload["outcome"]
        assert result["release"] == "noble"

        async def ids(**filters):
            payload = result_json(
                await client.call_tool("job_results", filters)
            )
            return [r["job_id"] for r in payload["results"]]

        assert job_id in await ids(feature="features/cli/attach.feature")
        assert job_id not in await ids(
            feature="features/cli/does-not-exist.feature"
        )
        assert job_id in await ids(release="noble")
        assert job_id not in await ids(release="jammy")
        assert job_id in await ids(machine_type="lxd-container")
        assert job_id not in await ids(machine_type="lxd-vm")
        assert await ids(job_ids=[job_id], state="finished") == [job_id]
        assert await ids(job_ids=[job_id], state="running") == []
