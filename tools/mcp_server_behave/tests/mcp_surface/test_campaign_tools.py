"""Campaign tools over the real MCP protocol, in process."""

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from behave_mcp.server import mcp
from tests.conftest import result_error_text, result_json

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
    monkeypatch.setenv("MCP_CAMPAIGN_DIR", str(tmp_path / "campaigns"))
    return repo_root


@pytest.mark.asyncio
async def test_create_campaign_plans_units_without_starting_them(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567"}
        )

    campaign = result_json(result)["campaign"]

    assert campaign["campaign_id"] == "1234567"
    assert campaign["total_units"] == 2
    assert campaign["counts"]["unattempted"] == 2
    assert campaign["counts"]["running"] == 0


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
    # MCP_MAX_PARALLEL_JOBS defaults to 1, so 8 lanes cannot be honoured.
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "create_campaign", {"campaign_id": "1234567", "max_lanes": 8}
        )

    assert "concurrent job limit" in result_error_text(result)


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
async def test_campaign_status_omits_units_by_default(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    assert payload["units"] is None
    assert payload["running"] == []
    assert payload["problems"] == []
    assert payload["campaign"]["counts"]["unattempted"] == 2


@pytest.mark.asyncio
async def test_campaign_status_returns_units_when_a_limit_is_given(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status",
            {"campaign_id": "1234567", "units_limit": 50},
        )

    units = result_json(result)["units"]

    assert len(units) == 2
    assert {unit["release"] for unit in units} == {"jammy", "noble"}
    assert all(unit["state"] == "unattempted" for unit in units)


@pytest.mark.asyncio
async def test_campaign_status_filters_by_release(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "campaign_status",
            {
                "campaign_id": "1234567",
                "release": "noble",
                "units_limit": 50,
            },
        )

    payload = result_json(result)

    assert payload["campaign"]["counts"]["unattempted"] == 1
    assert [unit["release"] for unit in payload["units"]] == ["noble"]


@pytest.mark.asyncio
async def test_campaign_status_on_an_unknown_campaign_is_an_error(repo):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "campaign_status", {"campaign_id": "absent"}
        )

    assert "no campaign" in result_error_text(result)


class StubLanes:
    """Stands in for real behave jobs: starts them, finishes on first poll."""

    def __init__(self):
        self.started = []
        self.done = set()

    def start(self, unit, *, repo_root, install_from):
        job_id = "job{}".format(len(self.started) + 1)
        self.started.append(unit)
        return job_id

    def poll(self, job_id):
        if job_id not in self.done:
            self.done.add(job_id)
            return None
        return {
            "status": "completed",
            "ok": True,
            "job_id": job_id,
            "summary": {
                "scenarios": {"passed": 1, "failed": 0, "skipped": 0},
                "features": {},
            },
        }


class StubTicker:
    """Captures the tick callable so no thread runs during a test."""

    def __init__(self):
        self.tick = None

    def start(self, tick):
        self.tick = tick

    def stop(self):
        self.tick = None

    def is_running(self):
        return self.tick is not None


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

    lanes = StubLanes()
    ticker = StubTicker()
    # The same log the server's own service reads, so events the runner
    # emits are visible to await_campaign_events.
    events = JsonlEventLog(tmp_path / "campaigns")
    replacement = CampaignRunner(
        store=JsonlCampaignStore(tmp_path / "campaigns"),
        lanes=lanes,
        lock=_FakeLock(),
        events=events,
        now=system_now,
        ticker=ticker,
    )
    monkeypatch.setattr(server_module, "_runner", replacement)
    monkeypatch.setattr(server_module, "_events", events)
    replacement.lanes = lanes
    replacement.ticker = ticker
    return replacement


class _FakeLock:
    def acquire(self, campaign_id):
        pass

    def release(self, campaign_id):
        pass

    def held_by_other(self, campaign_id):
        return False


@pytest.mark.asyncio
async def test_start_campaign_marks_it_running(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "start_campaign", {"campaign_id": "1234567"}
        )

    payload = result_json(result)

    assert payload["lifecycle"] == "running"
    assert payload["campaign_id"] == "1234567"


@pytest.mark.asyncio
async def test_starting_an_unknown_campaign_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool(
            "start_campaign", {"campaign_id": "absent"}
        )

    assert "no campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_only_one_campaign_runs_at_a_time(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "111"})
        await client.call_tool("create_campaign", {"campaign_id": "222"})
        await client.call_tool("start_campaign", {"campaign_id": "111"})
        result = await client.call_tool(
            "start_campaign", {"campaign_id": "222"}
        )

    assert "only one campaign" in result_error_text(result)


@pytest.mark.asyncio
async def test_pause_then_resume_round_trips(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})

        paused = await client.call_tool(
            "pause_campaign", {"campaign_id": "1234567"}
        )
        resumed = await client.call_tool(
            "resume_campaign", {"campaign_id": "1234567"}
        )

    assert result_json(paused)["lifecycle"] == "paused"
    assert result_json(resumed)["lifecycle"] == "running"


@pytest.mark.asyncio
async def test_pausing_a_campaign_that_never_started_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "pause_campaign", {"campaign_id": "1234567"}
        )

    assert "created" in result_error_text(result)


@pytest.mark.asyncio
async def test_cancel_closes_the_campaign(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})
        cancelled = await client.call_tool(
            "cancel_campaign", {"campaign_id": "1234567"}
        )
        restart = await client.call_tool(
            "start_campaign", {"campaign_id": "1234567"}
        )

    assert result_json(cancelled)["lifecycle"] == "cancelled"
    assert "cancelled" in result_error_text(restart)


@pytest.mark.asyncio
async def test_a_started_campaign_fills_lanes_on_its_tick(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool(
            "create_campaign", {"campaign_id": "1234567", "max_lanes": 1}
        )
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})
        # The ticker is a stub, so drive the tick the way the thread would.
        runner.ticker.tick()
        status = await client.call_tool(
            "campaign_status", {"campaign_id": "1234567"}
        )

    payload = result_json(status)

    assert len(runner.lanes.started) == 1
    assert payload["campaign"]["counts"]["running"] == 1
    assert len(payload["running"]) == 1


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
    assert payload["lifecycle"] == "created"


@pytest.mark.asyncio
async def test_await_events_follows_a_campaign_through_a_lane(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool(
            "create_campaign", {"campaign_id": "1234567", "max_lanes": 1}
        )
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})
        # Open the lane, poll it once while it is still going, then poll it
        # again once the stub reports it finished.
        for _ in range(3):
            runner.ticker.tick()

        result = await client.call_tool(
            "await_campaign_events",
            {"campaign_id": "1234567", "kinds": ["unit.*"]},
        )

    payload = result_json(result)

    assert [e["kind"] for e in payload["events"]] == ["unit.passed"]
    assert payload["campaign"]["counts"]["passed"] == 1


@pytest.mark.asyncio
async def test_the_cursor_reads_the_stream_without_repeats(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})

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
        "campaign.started",
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
    assert payload["campaign"]["counts"]["unattempted"] == 2
    assert payload["lifecycle"] == "created"


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
async def test_retry_units_requeues_a_failure(repo, runner, monkeypatch):
    def failing(job_id):
        if job_id not in runner.lanes.done:
            runner.lanes.done.add(job_id)
            return None
        return {
            "status": "completed",
            "ok": False,
            "job_id": job_id,
            "summary": {
                "scenarios": {"passed": 0, "failed": 1, "skipped": 0},
                "features": {},
            },
            "failures": [
                {
                    "step": "Then it works",
                    "status": "failed",
                    "error_message": "it did not",
                }
            ],
        }

    monkeypatch.setattr(runner.lanes, "poll", failing)

    async with create_connected_server_and_client_session(mcp) as client:
        # max_lanes stays at 1: MCP_MAX_PARALLEL_JOBS defaults to 1, and a
        # campaign asking for more than the server allows is refused.
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})
        # Two units through one lane: open, poll, finish-and-open, poll,
        # finish.
        for _ in range(5):
            runner.ticker.tick()

        status = result_json(
            await client.call_tool(
                "campaign_status", {"campaign_id": "1234567"}
            )
        )
        assert status["campaign"]["counts"]["failed"] == 2

        retried = result_json(
            await client.call_tool(
                "retry_units",
                {"campaign_id": "1234567", "reason": "maybe flaky"},
            )
        )

    assert retried["requeued"] == 2
    assert retried["rescheduling"] is True
    assert retried["lifecycle"] == "running"


@pytest.mark.asyncio
async def test_retry_units_with_nothing_to_retry_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("create_campaign", {"campaign_id": "1234567"})
        await client.call_tool("start_campaign", {"campaign_id": "1234567"})
        result = await client.call_tool(
            "retry_units", {"campaign_id": "1234567"}
        )

    assert "nothing was re-queued" in result_error_text(result)


@pytest.mark.asyncio
async def test_killing_an_unknown_job_is_an_error(repo, runner):
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("kill_job", {"job_id": "nope"})

    assert result.isError
