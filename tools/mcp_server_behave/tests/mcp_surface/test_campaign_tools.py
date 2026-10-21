"""Campaign tools over the real MCP protocol, in process."""

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from behave_mcp.server import mcp
from tests.conftest import (
    FakeLanes,
    FakeLock,
    FakeTicker,
    result_error_text,
    result_json,
)

pytestmark = pytest.mark.integration

FEATURE = """Feature: Example feature

  Scenario Outline: Runs everywhere
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-infra` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
      | noble   | lxd-vm        |
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A minimal checkout, with campaign files kept out of it."""
    repo_root = tmp_path / "repo"
    features = repo_root / "features"
    features.mkdir(parents=True)
    (features / "example.feature").write_text(FEATURE)
    (repo_root / "tox.ini").write_text("[tox]\n")
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    return repo_root


@pytest.mark.asyncio
async def test_create_campaign_plans_units_without_starting_them(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    assert payload["campaign"]["campaign_id"] == "1234567"
    assert payload["campaign"]["total_units"] == 2
    assert payload["summary"]["counts"]["unattempted"] == 2
    assert payload["summary"]["counts"]["in_flight"] == 0
    assert payload["summary"]["state"] == "created"


@pytest.mark.asyncio
async def test_create_campaign_records_scope_and_install_source(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {
                "campaign_id": "1234567",
                "releases": ["jammy"],
                "install_from": "proposed",
            },
        )

    campaign = result_json(result)["campaign"]

    assert campaign["total_units"] == 1
    assert campaign["scope"]["release"] == ["jammy"]
    assert campaign["install_from"] == "proposed"


@pytest.mark.asyncio
async def test_creating_the_same_campaign_twice_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567"}
        )

    assert "already holds a campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_more_lanes_than_the_server_allows_is_an_error(repo):
    # MCP_MAX_LXD_JOBS defaults to 1, so 8 lanes cannot be honoured.
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {"campaign_id": "1234567", "lanes": {"lxd": 8}},
        )

    assert "lxd job limit of 1" in result_error_text(result)


@pytest.mark.asyncio
async def test_more_than_one_wsl_lane_is_refused_by_the_schema(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {"campaign_id": "1234567", "lanes": {"wsl": 2}},
        )

    assert "less than or equal to 1" in result_error_text(result)


@pytest.mark.asyncio
async def test_an_unknown_release_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign",
            {"campaign_id": "1234567", "releases": ["bogus"]},
        )

    assert "unknown release" in result_error_text(result)


@pytest.mark.asyncio
async def test_list_campaigns_reports_created_campaigns(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "111"})
        await client.call_tool(
            "create_campaign",
            {"campaign_id": "222", "releases": ["jammy"]},
        )
        result = await client.call_tool("list_campaigns", {})

    campaigns = result_json(result)["campaigns"]

    assert [c["campaign_id"] for c in campaigns] == ["111", "222"]
    assert [c["total_units"] for c in campaigns] == [2, 1]


@pytest.mark.asyncio
async def test_list_campaigns_is_empty_before_any_exist(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("list_campaigns", {})

    assert result_json(result)["campaigns"] == []


@pytest.mark.asyncio
async def test_campaign_status_of_an_unknown_id_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "absent"}
        )

    # Never an empty record with header defaults that reads like a real one.
    assert result.isError
    assert "absent" in result_error_text(result)


@pytest.mark.asyncio
async def test_campaign_status_is_a_summary(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    # Individual units are unit_history's job.
    assert "units" not in payload
    assert payload["in_flight"] == []
    assert payload["problems"] == []
    assert payload["summary"]["counts"]["unattempted"] == 2


@pytest.mark.asyncio
async def test_campaign_status_filters_by_release(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status",
            {"campaign_id": "1234567", "release": "noble"},
        )

    payload = result_json(result)

    assert payload["summary"]["counts"]["unattempted"] == 1
    assert "units" not in payload


@pytest.mark.asyncio
async def test_campaign_status_on_an_unknown_campaign_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "absent"}
        )

    assert "no campaign" in result_error_text(result)


@pytest.fixture
def runner(repo, monkeypatch, tmp_path):
    """Replace the server's runner with one that launches no real jobs."""
    import behave_mcp.server as server_module
    from behave_campaign.adapters import (
        JsonlCampaignStore,
        JsonlEventLog,
        system_now,
    )
    from behave_campaign.runner import CampaignRunner

    lanes = FakeLanes(auto_finish={})
    ticker = FakeTicker()
    # The same log the server's own service reads, so events the runner
    # emits are visible to await_campaign_events.
    events = JsonlEventLog(tmp_path / "state" / "campaigns")
    replacement = CampaignRunner(
        store=JsonlCampaignStore(tmp_path / "state" / "campaigns"),
        lanes=lanes,
        lock=FakeLock(),
        events=events,
        now=system_now,
        ticker=ticker,
    )
    monkeypatch.setattr(server_module, "_runner", replacement)
    monkeypatch.setattr(server_module, "_events", events)
    replacement.lanes = lanes
    replacement.ticker = ticker
    return replacement


@pytest.mark.asyncio
async def test_run_campaign_marks_it_running(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )

    payload = result_json(result)

    assert payload["state"] == "running"
    assert payload["intent"] == "run"
    assert payload["campaign_id"] == "1234567"


@pytest.mark.asyncio
async def test_running_an_unknown_campaign_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "control_campaign", {"campaign_id": "absent", "action": "run"}
        )

    assert "no campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_only_one_campaign_runs_at_a_time(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "111"})
        await client.call_tool("create_campaign", {"campaign_id": "222"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "111", "action": "run"}
        )
        result = await client.call_tool(
            "control_campaign", {"campaign_id": "222", "action": "run"}
        )

    assert "only one campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_pause_then_run_round_trips(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )

        paused = await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "pause"}
        )
        resumed = await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )

    assert result_json(paused)["state"] == "paused"
    assert result_json(paused)["intent"] == "hold"
    assert result_json(resumed)["state"] == "running"


@pytest.mark.asyncio
async def test_pausing_a_campaign_that_never_ran_holds_it(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "pause"}
        )

    assert result_json(result)["state"] == "paused"


@pytest.mark.asyncio
async def test_pause_with_kill_interrupts_the_jobs_in_flight(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        # One tick opens the one lane, and nothing finishes it.
        runner.lanes.auto_finish = None
        runner.ticker.tick()
        result = await client.call_tool(
            "control_campaign",
            {"campaign_id": "1234567", "action": "pause", "kill": True},
        )

    payload = result_json(result)

    assert payload["state"] == "paused"
    assert [u["job_id"] for u in payload["interrupted"]] == ["job1"]
    assert runner.lanes.killed == ["job1"]


@pytest.mark.asyncio
async def test_kill_only_applies_to_pause(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "control_campaign",
            {"campaign_id": "1234567", "action": "run", "kill": True},
        )

    assert "kill applies only to pause" in result_error_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "resume", "cancel"])
async def test_an_old_verb_is_refused(repo, runner, action):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": action}
        )

    assert result.isError


@pytest.mark.asyncio
async def test_a_running_campaign_fills_lanes_on_its_tick(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        # One tick only: this is about the lane being open, not the campaign
        # finishing.
        runner.ticker.tick()
        status = await client.call_tool(
            "campaign_status", {"campaign_id": "1234567"}
        )

    payload = result_json(status)

    assert len(runner.lanes.started) == 1
    assert payload["summary"]["counts"]["in_flight"] == 1
    assert len(payload["in_flight"]) == 1


@pytest.mark.asyncio
async def test_await_events_reports_the_campaign_being_created(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "await_campaign_events", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    assert [e["kind"] for e in payload["events"]] == ["campaign.created"]
    assert payload["next_seq"] == 1
    assert payload["state"] == "created"


@pytest.mark.asyncio
async def test_unit_history_lists_every_attempt(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        runner.ticker.drive()

        result = await client.call_tool(
            "unit_history", {"campaign_id": "1234567", "release": "jammy"}
        )

    payload = result_json(result)

    (unit,) = payload["units"]
    assert unit["release"] == "jammy"
    assert unit["state"] == "passed"
    (attempt,) = unit["attempts"]
    assert attempt["outcome"] == "passed"
    assert attempt["job_id"]


@pytest.mark.asyncio
async def test_await_events_defaults_to_what_needs_acting_on(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        runner.ticker.drive()

        result = await client.call_tool(
            "await_campaign_events", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    # Two passes and two lane openings happened; the default keeps the
    # campaign's own transitions and leaves progress to the counts.
    assert [e["kind"] for e in payload["events"]] == [
        "campaign.created",
        "campaign.running",
        "campaign.finishing",
        "campaign.complete",
    ]
    assert payload["counts"]["passed"] == 2


@pytest.mark.asyncio
async def test_await_events_follows_a_campaign_through_a_lane(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        runner.ticker.drive()

        result = await client.call_tool(
            "await_campaign_events",
            {"campaign_id": "1234567", "kinds": ["unit.*"]},
        )

    payload = result_json(result)

    # Every unit in the fixture feature, run to completion.
    assert [e["kind"] for e in payload["events"]] == [
        "unit.passed",
        "unit.passed",
    ]
    assert payload["counts"]["passed"] == 2
    assert payload["counts"]["unattempted"] == 0
    assert payload["state"] == "complete"


@pytest.mark.asyncio
async def test_the_cursor_reads_the_stream_without_repeats(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )

        first = result_json(
            await client.call_tool(
                "await_campaign_events", {"campaign_id": "1234567"}
            )
        )
        second = result_json(
            await client.call_tool(
                "await_campaign_events",
                {"campaign_id": "1234567", "since_seq": first["next_seq"]},
            )
        )

    assert [e["kind"] for e in first["events"]] == [
        "campaign.created",
        "campaign.running",
    ]
    assert second["events"] == []
    assert second["timed_out"] is True


@pytest.mark.asyncio
async def test_an_empty_batch_still_says_where_things_stand(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "await_campaign_events",
            {"campaign_id": "1234567", "since_seq": 99},
        )

    payload = result_json(result)

    # This is why there is no heartbeat event: a quiet poll is informative.
    assert payload["events"] == []
    assert payload["counts"]["unattempted"] == 2
    assert payload["state"] == "created"


@pytest.mark.asyncio
async def test_an_unknown_event_kind_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "await_campaign_events",
            {"campaign_id": "1234567", "kinds": ["unit.exploded"]},
        )

    assert "unknown event kind" in result_error_text(result)


@pytest.mark.asyncio
async def test_a_timeout_beyond_the_server_limit_is_refused(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "await_campaign_events",
            {"campaign_id": "1234567", "timeout_seconds": 9999},
        )

    assert "exceeds this server's limit" in result_error_text(result)


@pytest.mark.asyncio
async def test_retry_units_requeues_a_failure(repo, runner):
    runner.lanes.auto_finish = dict(
        passed=0,
        failed=1,
        failures=[
            {
                "step": "Then it works",
                "status": "failed",
                "error_message": "it did not",
            }
        ],
    )

    async with create_connected_server_and_client_session(mcp) as client:
        # lanes stay at 1: MCP_MAX_LXD_JOBS defaults to 1, and a
        # campaign asking for more than the server allows is refused.
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        runner.ticker.drive()

        status = result_json(
            await client.call_tool(
                "campaign_status", {"campaign_id": "1234567"}
            )
        )
        assert status["summary"]["counts"]["failed"] == 2

        retried = result_json(
            await client.call_tool(
                "retry_units",
                {"campaign_id": "1234567", "reason": "maybe flaky"},
            )
        )

    assert retried["requeued"] == 2
    assert retried["state"] == "running"
    assert retried["intent"] == "run"


@pytest.mark.asyncio
async def test_retry_units_with_nothing_to_retry_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool(
            "control_campaign", {"campaign_id": "1234567", "action": "run"}
        )
        result = await client.call_tool(
            "retry_units", {"campaign_id": "1234567"}
        )

    assert "nothing was re-queued" in result_error_text(result)


@pytest.mark.asyncio
async def test_killing_an_unknown_job_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("kill_job", {"job_id": "nope"})

    assert result.isError
