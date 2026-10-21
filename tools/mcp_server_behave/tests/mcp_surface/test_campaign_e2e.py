"""A real campaign, driven by the real scheduler, running real behave jobs.

Everything below the campaign is genuine: tox starts, behave runs against the
actual feature corpus, a JSON report lands on disk, and the runner classifies
and records it. Only the passage of time is waited on.

The point is concurrency. Every other test fakes the lane runner, so nothing
else proves that several behave subprocesses really do run at once and that
the sliding window keeps them filled.

``features/_version.feature``'s attached-version scenario is used because it
is cheap without a contract token: it carries ``@uses.config.contract_token``,
so without that config it skips in ``before_scenario``, before any machine is
provisioned. A skip is a
perfectly good outcome here -- what is under test is the scheduler, not the
scenario. On a host with LXD and the config set, the same campaign provisions
containers and the assertions still hold.
"""

import os
import time
from pathlib import Path

import pytest

from behave_campaign.adapters import (
    FileCampaignRunLock,
    JsonlCampaignStore,
    ParserFeatureReader,
    ServiceLaneRunner,
    system_now,
)
from behave_campaign.domain.feed import derive_feed
from behave_campaign.domain.records import CampaignState, UnitState
from behave_campaign.domain.state import reduce_units, state_of
from behave_campaign.repo import repo_state
from behave_campaign.runner import CampaignRunner, ThreadTicker
from behave_campaign.service import CampaignService
from behave_mcp.config import load_settings
from behave_mcp.domain.pools import Pool
from behave_mcp.server import create_service

FEATURE = "features/_version.feature"
# Naming one scenario keeps the campaign to exactly one unit per release, so
# the lane arithmetic is unambiguous.
SCENARIO = "Attached show version in a ubuntu machine"
RELEASES = ["focal", "jammy", "noble"]
LANES = 3
CAMPAIGN_ID = "e2e-concurrency"

# Three tox/behave starts, each of which installs into its own environment.
CAMPAIGN_TIMEOUT_SECONDS = 1800
SAMPLE_INTERVAL_SECONDS = 0.5

TERMINAL_STATES = {"passed", "failed", "skipped", "error"}


@pytest.mark.e2e
@pytest.mark.long_running
def test_a_campaign_runs_its_lanes_concurrently(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))

    campaign_dir = tmp_path / "state" / "campaigns"
    store = JsonlCampaignStore(campaign_dir)

    # A service of its own, so the lane limit is this test's rather than
    # whatever the server process was started with.
    settings = load_settings({**os.environ, "MCP_MAX_LXD_JOBS": str(LANES)})
    service = create_service(settings)

    created = CampaignService(
        store=store,
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
        lane_ceiling=settings.pool_limits,
    ).create_campaign(
        campaign_id=CAMPAIGN_ID,
        repo_root=repo_root,
        releases=RELEASES,
        machine_types=["lxd-container"],
        feature_files=[FEATURE],
        scenarios=[SCENARIO],
        lanes={Pool.LXD: LANES},
    )
    assert created.campaign.total_units == len(RELEASES)

    runner = CampaignRunner(
        store=store,
        lanes=ServiceLaneRunner(service),
        lock=FileCampaignRunLock(campaign_dir),
        now=system_now,
        ticker=ThreadTicker(interval=1.0),
    )

    peak_lanes = 0
    try:
        runner.run(CAMPAIGN_ID)
        deadline = time.monotonic() + CAMPAIGN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            statuses = reduce_units(store.replay(CAMPAIGN_ID))
            peak_lanes = max(
                peak_lanes,
                sum(1 for s in statuses if s.state == UnitState.IN_FLIGHT),
            )
            if runner.active_campaign() is None:
                break
            time.sleep(SAMPLE_INTERVAL_SECONDS)
    finally:
        runner.shutdown()

    records = store.replay(CAMPAIGN_ID)
    statuses = reduce_units(records)
    states = {s.unit.release: s.state for s in statuses}

    assert (
        state_of(records) == CampaignState.COMPLETE
    ), "campaign did not finish within {}s; states were {}".format(
        CAMPAIGN_TIMEOUT_SECONDS, states
    )
    # The reason this test exists: the window really did fill.
    assert peak_lanes >= LANES, (
        "only ever saw {} of {} lanes in flight at once; the sliding window "
        "is not filling to capacity".format(peak_lanes, LANES)
    )
    assert set(states) == set(RELEASES)
    assert all(state in TERMINAL_STATES for state in states.values()), states

    # Each unit was attempted exactly once, and that attempt finished.
    for status in statuses:
        assert len(status.attempts) == 1, status
        attempt = status.attempts[0]
        assert not attempt.in_flight
        assert attempt.outcome == status.state
        assert attempt.started_at and attempt.finished_at

    # Every recorded job left artifacts behind to inspect.
    for status in statuses:
        assert status.job_id
        job_dir = tmp_path / "state" / "jobs" / status.job_id
        for name in ("job.json", "status.json", "report.json", "stdout.log"):
            assert (job_dir / name).is_file(), job_dir / name

    # The stream a watching agent would have seen, from real jobs.
    stream = derive_feed(CAMPAIGN_ID, store.replay(CAMPAIGN_ID))
    kinds = [event.kind for event in stream]

    assert kinds[0] == "campaign.created"
    assert "campaign.running" in kinds
    assert kinds[-1] == "campaign.complete"
    assert kinds.count("lane.started") == len(RELEASES)
    assert sum(1 for kind in kinds if kind.startswith("unit.")) == len(
        RELEASES
    )
    # seq is dense and monotonic, so a cursor cannot skip anything.
    assert [event.seq for event in stream] == list(range(1, len(stream) + 1))


KILL_CAMPAIGN_ID = "e2e-pause-with-kill"


def _wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(SAMPLE_INTERVAL_SECONDS)
    raise AssertionError(
        "timed out after {}s waiting for {}".format(timeout, what)
    )


@pytest.mark.e2e
@pytest.mark.long_running
def test_pause_with_kill_stops_a_real_job_and_run_redoes_it(
    monkeypatch, tmp_path
):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))

    campaign_dir = tmp_path / "state" / "campaigns"
    store = JsonlCampaignStore(campaign_dir)
    settings = load_settings({**os.environ, "MCP_MAX_LXD_JOBS": "1"})
    service = create_service(settings)
    CampaignService(
        store=store,
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
        lane_ceiling=settings.pool_limits,
    ).create_campaign(
        campaign_id=KILL_CAMPAIGN_ID,
        repo_root=repo_root,
        releases=[RELEASES[0]],
        machine_types=["lxd-container"],
        feature_files=[FEATURE],
        scenarios=[SCENARIO],
        lanes={Pool.LXD: 1},
    )
    runner = CampaignRunner(
        store=store,
        lanes=ServiceLaneRunner(service),
        lock=FileCampaignRunLock(campaign_dir),
        now=system_now,
        ticker=ThreadTicker(interval=1.0),
    )

    def unit():
        (status,) = reduce_units(store.replay(KILL_CAMPAIGN_ID))
        return status

    try:
        runner.run(KILL_CAMPAIGN_ID)
        _wait_for(
            lambda: unit().state == UnitState.IN_FLIGHT,
            CAMPAIGN_TIMEOUT_SECONDS,
            "the job to start",
        )
        killed_job = unit().job_id

        paused = runner.pause(KILL_CAMPAIGN_ID, kill=True)

        assert paused.state == CampaignState.PAUSED
        assert paused.unsignalled == []
        assert unit().attempts[-1].interrupted
        # The job's process group really went away.
        _wait_for(
            lambda: service.job_status(killed_job).status == "completed",
            CAMPAIGN_TIMEOUT_SECONDS,
            "the killed job to exit",
        )

        runner.run(KILL_CAMPAIGN_ID)
        _wait_for(
            lambda: runner.active_campaign() is None,
            CAMPAIGN_TIMEOUT_SECONDS,
            "the campaign to complete",
        )
    finally:
        runner.shutdown()

    final = unit()
    assert state_of(store.replay(KILL_CAMPAIGN_ID)) == CampaignState.COMPLETE
    assert [a.interrupted for a in final.attempts] == [True, False]
    assert final.attempts[0].job_id == killed_job
    assert final.state in TERMINAL_STATES
