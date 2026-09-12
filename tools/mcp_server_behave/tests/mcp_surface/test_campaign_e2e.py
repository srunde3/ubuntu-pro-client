"""A real campaign, driven by the real scheduler, running real behave jobs.

Everything below the campaign is genuine: tox starts, behave runs against the
actual feature corpus, a JSON report lands on disk, and the runner classifies
and records it. Only the passage of time is waited on.

The point is concurrency. Every other test fakes the lane runner, so nothing
else proves that several behave subprocesses really do run at once and that
the sliding window keeps them filled.

``features/_version.feature`` is used because it needs no contract token: its
scenarios carry ``@uses.config.check_version``, so without that config they
skip in ``before_scenario``, before any machine is provisioned. A skip is a
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
    JsonlEventLog,
    ParserFeatureReader,
    ServiceLaneRunner,
    system_now,
)
from behave_campaign.domain import COMPLETE, lifecycle, reduce_units
from behave_campaign.repo import repo_state
from behave_campaign.runner import CampaignRunner, ThreadTicker
from behave_campaign.service import CampaignService
from behave_mcp.adapters import InMemoryJobRegistry
from behave_mcp.config import load_settings
from behave_mcp.server import create_service

FEATURE = "features/_version.feature"
# The feature holds four scenario outlines; naming one keeps the campaign to
# exactly one unit per release, so the lane arithmetic is unambiguous.
SCENARIO = "Check pro version"
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
    monkeypatch.setenv("MCP_LOG_DIR", str(tmp_path / "logs"))

    campaign_dir = tmp_path / "campaigns"
    store = JsonlCampaignStore(campaign_dir)
    events = JsonlEventLog(campaign_dir)

    # A service of its own, so the lane limit is this test's rather than
    # whatever the server process was started with.
    settings = load_settings(
        {**os.environ, "MCP_MAX_PARALLEL_JOBS": str(LANES)}
    )
    service = create_service(settings, registry=InMemoryJobRegistry())

    created = CampaignService(
        store=store,
        features=ParserFeatureReader(),
        events=events,
        now=system_now,
        repo_state=repo_state,
        max_lane_ceiling=settings.max_parallel_jobs,
    ).create_campaign(
        campaign_id=CAMPAIGN_ID,
        repo_root=repo_root,
        releases=RELEASES,
        machine_types=["lxd-container"],
        feature_files=[FEATURE],
        scenarios=[SCENARIO],
        max_lanes=LANES,
    )
    assert created.campaign.total_units == len(RELEASES)

    runner = CampaignRunner(
        store=store,
        lanes=ServiceLaneRunner(service),
        lock=FileCampaignRunLock(campaign_dir),
        events=events,
        now=system_now,
        ticker=ThreadTicker(interval=1.0),
    )

    peak_lanes = 0
    try:
        runner.start(CAMPAIGN_ID)
        deadline = time.monotonic() + CAMPAIGN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            statuses = reduce_units(store.replay(CAMPAIGN_ID))
            peak_lanes = max(
                peak_lanes,
                sum(1 for s in statuses if s.state == "running"),
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
        lifecycle(records) == COMPLETE
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

    # Each unit was attempted exactly once: a running attempt when its lane
    # opened, then the outcome. No unit was scheduled twice.
    for status in statuses:
        assert [a.state for a in status.attempts][0] == "running"
        assert len(status.attempts) == 2, status

    # Every recorded job left artifacts behind to inspect.
    for status in statuses:
        assert status.job_id
        report = tmp_path / "logs" / "{}_report.json".format(status.job_id)
        assert report.is_file(), report

    # The stream a watching agent would have seen, from real jobs.
    stream = events.read(CAMPAIGN_ID, since_seq=0, kinds=[], limit=500)
    kinds = [event.kind for event in stream]

    assert kinds[0] == "campaign.created"
    assert "campaign.started" in kinds
    assert kinds[-1] == "campaign.complete"
    assert kinds.count("lane.started") == len(RELEASES)
    assert kinds.count("lane.released") == len(RELEASES)
    assert sum(1 for kind in kinds if kind.startswith("unit.")) == len(
        RELEASES
    )
    # seq is dense and monotonic, so a cursor cannot skip anything.
    assert [event.seq for event in stream] == list(range(1, len(stream) + 1))

    persisted = campaign_dir / "{}.events.jsonl".format(CAMPAIGN_ID)
    assert persisted.is_file()
    assert len(persisted.read_text().splitlines()) == len(stream)
