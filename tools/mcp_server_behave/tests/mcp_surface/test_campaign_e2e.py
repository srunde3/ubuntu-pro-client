"""A real campaign running real behave jobs.

Everything below the campaign is genuine: tox starts, behave runs against the
actual feature corpus, a JSON report lands on disk, and each job's own
process starts the next queued job when it ends. While it waits, the test
reconciles every half second, as the MCP server's timer does, which records
outcomes and queues the campaign's next units.

``features/_version.feature``'s attached-version scenario is used because it
is cheap without a contract token: it carries ``@uses.config.contract_token``,
so without that config it skips in ``before_scenario``, before any machine is
provisioned. A skip is a perfectly good outcome here -- what is under test
is the scheduling, not the scenario.
"""

import time
from pathlib import Path

import pytest

from behave_campaign.adapters import (
    JsonlCampaignStore,
    ParserFeatureReader,
    ServiceJobRunner,
    system_now,
)
from behave_campaign.domain.feed import derive_feed
from behave_campaign.domain.records import CampaignState, UnitState
from behave_campaign.domain.state import reduce_units, state_of
from behave_campaign.repo import repo_state
from behave_campaign.service import CampaignService
from behave_mcp.domain.pools import Pool
from behave_mcp.server import create_service
from behave_mcp.service import BehaveServiceError, UnknownJobError

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


def campaigns_over(service, store):
    """The campaign service every entry point builds, over ``service``."""
    return CampaignService(
        store=store,
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
        max_jobs_ceiling=service.read_config().max_jobs,
        jobs=ServiceJobRunner(
            service, refusal=BehaveServiceError, unknown_job=UnknownJobError
        ),
    )


def _wait_for(predicate, timeout, what, meanwhile=lambda: None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        meanwhile()
        time.sleep(SAMPLE_INTERVAL_SECONDS)
    raise AssertionError(
        "timed out after {}s waiting for {}".format(timeout, what)
    )


def _most_at_once(spans):
    """The most (start, end) spans that overlap at any moment."""
    moments = sorted(
        [(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans]
    )
    running = most = 0
    for _, change in moments:
        running += change
        most = max(most, running)
    return most


@pytest.mark.e2e
@pytest.mark.long_running
def test_a_campaign_runs_its_jobs_side_by_side(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))

    store = JsonlCampaignStore(tmp_path / "state" / "campaigns")
    # The test's own state directory, so max jobs is this test's.
    service = create_service()
    service.set_config("max_jobs.lxd", str(LANES))
    campaigns = campaigns_over(service, store)
    created = campaigns.create_campaign(
        campaign_id=CAMPAIGN_ID,
        repo_root=repo_root,
        releases=RELEASES,
        machine_types=["lxd-container"],
        feature_files=[FEATURE],
        scenarios=[SCENARIO],
        max_jobs={Pool.LXD: LANES},
    )
    assert created.campaign.total_units == len(RELEASES)

    campaigns.run(campaign_id=CAMPAIGN_ID)
    _wait_for(
        lambda: state_of(store.read_records(CAMPAIGN_ID))
        == CampaignState.COMPLETE,
        CAMPAIGN_TIMEOUT_SECONDS,
        "the campaign to complete",
        meanwhile=campaigns.reconcile,
    )

    records = store.read_records(CAMPAIGN_ID)
    statuses = reduce_units(records)
    states = {s.unit.release: s.state for s in statuses}
    assert set(states) == set(RELEASES)
    assert all(state in TERMINAL_STATES for state in states.values()), states

    # Each unit was attempted exactly once, and that attempt finished.
    for status in statuses:
        assert len(status.attempts) == 1, status
        attempt = status.attempts[0]
        assert not attempt.in_flight
        assert attempt.outcome == status.state

    # The jobs really ran side by side, by their own clocks.
    spans = []
    for status in statuses:
        artifacts = service.get_artifacts(status.job_id)
        assert artifacts.record.campaign_id == CAMPAIGN_ID
        spans.append((artifacts.record.started_at, artifacts.exit.finished_at))
    assert _most_at_once(spans) == LANES, spans

    # Every recorded job left artifacts behind to inspect.
    for status in statuses:
        job_dir = tmp_path / "state" / "jobs" / status.job_id
        for name in ("job.json", "exit.json", "report.json", "stdout.log"):
            assert (job_dir / name).is_file(), job_dir / name

    # The stream a watching agent would have seen, from real jobs.
    stream = derive_feed(CAMPAIGN_ID, records)
    kinds = [event.kind for event in stream]
    assert kinds[0] == "campaign.created"
    assert kinds[-1] == "campaign.complete"
    assert kinds.count("job.started") == len(RELEASES)
    assert sum(1 for kind in kinds if kind.startswith("unit.")) == len(
        RELEASES
    )
    # seq is dense and monotonic, so a cursor cannot skip anything.
    assert [event.seq for event in stream] == list(range(1, len(stream) + 1))


KILL_CAMPAIGN_ID = "e2e-pause-with-kill"


@pytest.mark.e2e
@pytest.mark.long_running
def test_pause_with_kill_stops_a_real_job_and_run_redoes_it(
    monkeypatch, tmp_path
):
    repo_root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo_root))
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))

    store = JsonlCampaignStore(tmp_path / "state" / "campaigns")
    service = create_service()
    campaigns = campaigns_over(service, store)
    campaigns.create_campaign(
        campaign_id=KILL_CAMPAIGN_ID,
        repo_root=repo_root,
        releases=[RELEASES[0]],
        machine_types=["lxd-container"],
        feature_files=[FEATURE],
        scenarios=[SCENARIO],
        max_jobs={Pool.LXD: 1},
    )

    def unit():
        (status,) = reduce_units(store.read_records(KILL_CAMPAIGN_ID))
        return status

    def complete():
        records = store.read_records(KILL_CAMPAIGN_ID)
        return state_of(records) == CampaignState.COMPLETE

    try:
        campaigns.run(campaign_id=KILL_CAMPAIGN_ID)
        assert unit().state == UnitState.IN_FLIGHT
        killed_job = unit().job_id

        paused = campaigns.pause(campaign_id=KILL_CAMPAIGN_ID, kill=True)

        assert paused.state == CampaignState.PAUSED
        assert paused.not_killed == []
        assert unit().attempts[-1].interrupted
        # The job's process group really went away.
        _wait_for(
            lambda: service.get_artifacts(killed_job).exit is not None,
            CAMPAIGN_TIMEOUT_SECONDS,
            "the killed job to exit",
        )

        campaigns.run(campaign_id=KILL_CAMPAIGN_ID)
        _wait_for(
            complete,
            CAMPAIGN_TIMEOUT_SECONDS,
            "the campaign to end",
            meanwhile=campaigns.reconcile,
        )
    finally:
        # A job that outlives the test holds a lane.
        for status in reduce_units(store.read_records(KILL_CAMPAIGN_ID)):
            if status.in_flight and status.job_id:
                service.kill_job(status.job_id)

    final = unit()
    assert [a.interrupted for a in final.attempts] == [True, False]
    assert final.attempts[0].job_id == killed_job
    assert final.state in TERMINAL_STATES
