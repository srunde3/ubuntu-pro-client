import pytest

from behave_campaign.adapters import JsonlCampaignStore, JsonlEventLog
from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    Filters,
    RepoState,
    Unit,
)
from behave_campaign.service import CampaignService

AT = "2026-09-12T12:00:00Z"
REPO = RepoState(root="/repo", commit="abc123", branch="main", dirty=False)

UNITS = [
    Unit("features/a.feature", "A", "jammy", "lxd-container"),
    Unit("features/a.feature", "A", "noble", "lxd-vm"),
    Unit("features/b.feature", "B", "jammy", "lxd-container"),
]


class FakeFeatureReader:
    """Returns a fixed unit list, filtered the way discovery would."""

    def __init__(self, units=None):
        self.units = UNITS if units is None else units
        self.seen_filters = None

    def discover_units(self, repo_root, filters):
        self.seen_filters = filters
        return [unit for unit in self.units if filters.matches_unit(unit)]

    def available_dimensions(self, repo_root):
        return {
            "releases": [
                {"name": "jammy", "scenario_count": 2},
                {"name": "noble", "scenario_count": 1},
            ],
            "machine_types": [
                {"name": "lxd-container", "scenario_count": 2},
                {"name": "lxd-vm", "scenario_count": 1},
            ],
        }


@pytest.fixture
def features():
    return FakeFeatureReader()


@pytest.fixture
def events(tmp_path):
    return JsonlEventLog(tmp_path / "campaigns")


@pytest.fixture
def service(tmp_path, features, events):
    return CampaignService(
        store=JsonlCampaignStore(tmp_path / "campaigns"),
        features=features,
        events=events,
        now=lambda: AT,
        repo_state=lambda root: REPO,
        max_lane_ceiling=8,
    )


@pytest.fixture
def store(tmp_path):
    return JsonlCampaignStore(tmp_path / "campaigns")


def create(service, **overrides):
    fields = {"campaign_id": "1234567", "repo_root": "/repo"}
    fields.update(overrides)
    return service.create_campaign(**fields)


class TestCreateCampaign:
    def test_it_plans_every_matching_unit(self, service):
        result = create(service)

        assert result.campaign.total_units == 3
        assert result.campaign.counts.unattempted == 3
        assert result.campaign.counts.passed == 0

    def test_it_records_how_the_campaign_was_built(self, service):
        result = create(
            service, install_from="proposed", max_lanes=8, releases=["jammy"]
        )

        assert result.campaign.campaign_id == "1234567"
        assert result.campaign.created_at == AT
        assert result.campaign.install_from == "proposed"
        assert result.campaign.max_lanes == 8
        assert result.campaign.repo.commit == "abc123"
        assert result.campaign.scope.release == ["jammy"]

    def test_filters_narrow_the_plan(self, service):
        result = create(service, releases=["jammy"])

        assert result.campaign.total_units == 2

    def test_it_passes_every_filter_through(self, service, features):
        create(
            service,
            releases=["jammy"],
            machine_types=["lxd-container"],
            feature_files=["features/a.feature"],
            scenarios=["A"],
        )

        assert features.seen_filters == Filters(
            feature=("features/a.feature",),
            scenario=("A",),
            release=("jammy",),
            machine_type=("lxd-container",),
        )

    def test_it_starts_nothing(self, service, store):
        create(service)

        kinds = [type(record).__name__ for record in store.replay("1234567")]

        assert "AttemptRecord" not in kinds

    def test_a_scope_matching_nothing_is_rejected(self, service):
        with pytest.raises(CampaignError) as error:
            create(service, releases=["focal"])

        assert "no test units matched" in str(error.value)

    def test_an_unusable_campaign_id_is_rejected(self, service):
        with pytest.raises(CampaignError) as error:
            create(service, campaign_id="../escape")

        assert "campaign id" in str(error.value)

    def test_an_empty_campaign_id_is_rejected(self, service):
        with pytest.raises(CampaignError):
            create(service, campaign_id="")

    def test_an_unknown_install_source_is_rejected(self, service):
        with pytest.raises(CampaignError) as error:
            create(service, install_from="nightly")

        assert "install source" in str(error.value)

    def test_more_lanes_than_the_server_allows_is_rejected(self, service):
        with pytest.raises(CampaignError) as error:
            create(service, max_lanes=9)

        assert "exceeds the server's concurrent job limit of 8" in str(
            error.value
        )

    def test_the_server_limit_itself_is_allowed(self, service):
        assert create(service, max_lanes=8).campaign.max_lanes == 8

    def test_zero_lanes_is_rejected(self, service):
        with pytest.raises(CampaignError):
            create(service, max_lanes=0)


class TestListCampaigns:
    def test_it_summarises_each_stored_campaign(self, service):
        create(service, campaign_id="111")
        create(service, campaign_id="222", releases=["jammy"])

        result = service.list_campaigns()

        assert [c.campaign_id for c in result.campaigns] == ["111", "222"]
        assert [c.total_units for c in result.campaigns] == [3, 2]

    def test_it_reports_where_campaigns_live(self, service, store):
        create(service)

        assert result_dir(service) == str(store.root)

    def test_no_campaigns_is_an_empty_list(self, service):
        assert service.list_campaigns().campaigns == []


def result_dir(service):
    return service.list_campaigns().campaign_dir


class TestCampaignStatus:
    def test_a_fresh_campaign_is_all_unattempted(self, service):
        create(service)

        status = service.campaign_status(campaign_id="1234567")

        assert status.campaign.counts.unattempted == 3
        assert status.running == []
        assert status.problems == []

    def test_units_are_omitted_unless_asked_for(self, service):
        create(service)

        assert service.campaign_status(campaign_id="1234567").units is None

    def test_a_units_limit_returns_them(self, service):
        create(service)

        status = service.campaign_status(campaign_id="1234567", units_limit=10)

        assert len(status.units) == 3
        assert not status.truncated

    def test_the_unit_list_is_capped_and_says_so(self, service):
        create(service)

        status = service.campaign_status(campaign_id="1234567", units_limit=2)

        assert len(status.units) == 2
        assert status.truncated

    def test_an_oversized_limit_is_clamped(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", units_limit=10_000
        )

        assert status.limit_clamped

    def test_a_zero_units_limit_simply_lists_none(self, service):
        create(service)

        status = service.campaign_status(campaign_id="1234567", units_limit=0)

        assert status.units is None
        assert status.campaign.counts.unattempted == 3

    def test_a_negative_units_limit_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.campaign_status(campaign_id="1234567", units_limit=-1)

    def test_running_and_problem_units_are_always_reported(
        self, service, store
    ):
        create(service)
        store.append(
            "1234567",
            [
                AttemptStarted(
                    unit=UNITS[0],
                    job_id="job1",
                    install_from="proposed",
                    at=AT,
                ),
                AttemptFinished(
                    unit=UNITS[1], job_id="job2", outcome="failed", at=AT
                ),
            ],
        )

        status = service.campaign_status(campaign_id="1234567")

        assert [u.job_id for u in status.running] == ["job1"]
        assert [u.job_id for u in status.problems] == ["job2"]
        assert status.campaign.counts.running == 1
        assert status.campaign.counts.failed == 1
        assert status.campaign.counts.unattempted == 1

    def test_filters_narrow_the_counts(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", filters=Filters(release=("noble",))
        )

        assert status.campaign.counts.unattempted == 1

    def test_a_state_filter_selects_units(self, service, store):
        create(service)
        store.append(
            "1234567",
            [
                AttemptFinished(
                    unit=UNITS[0], job_id="job1", outcome="passed", at=AT
                )
            ],
        )

        status = service.campaign_status(
            campaign_id="1234567",
            filters=Filters(state=("passed",)),
            units_limit=10,
        )

        assert [u.state for u in status.units] == ["passed"]

    def test_it_reports_the_stored_header(self, service):
        create(service, install_from="proposed", max_lanes=4)

        status = service.campaign_status(campaign_id="1234567")

        assert status.campaign.install_from == "proposed"
        assert status.campaign.max_lanes == 4
        assert status.campaign.created_at == AT


class TestDimensions:
    def test_it_reports_releases_and_machine_types(self, service):
        result = service.dimensions(repo_root="/repo")

        assert [v.name for v in result.releases] == ["jammy", "noble"]
        assert result.releases[0].scenario_count == 2
        assert [v.name for v in result.machine_types] == [
            "lxd-container",
            "lxd-vm",
        ]


def record(service, unit, outcome, job_id, **kwargs):
    fields = {
        "campaign_id": "1234567",
        "payload": [
            {
                "feature": unit.feature,
                "scenario": unit.scenario,
                "release": unit.release,
                "machine_type": unit.machine_type,
                "outcome": outcome,
                "job_id": job_id,
            }
        ],
        "install_from": "proposed",
    }
    fields.update(kwargs)
    return service.record_attempts(**fields)


class TestRecordAttempts:
    def test_an_attempt_updates_the_unit_state(self, service):
        create(service)

        result = record(service, UNITS[0], "failed", "job1")

        assert result.recorded == 1
        assert result.campaign.counts.failed == 1
        assert [u.job_id for u in result.problems] == ["job1"]

    def test_it_stamps_the_install_source_and_time(self, service, store):
        create(service)
        record(service, UNITS[0], "passed", "job1")

        started, finished = store.replay("1234567")[-2:]

        # A completed try is written as both halves, with the source on the
        # start -- that is what the job actually ran with.
        assert started.install_from == "proposed"
        assert started.at == AT
        assert finished.outcome == "passed"
        assert finished.job_id == started.job_id

    def test_a_pass_supersedes_an_earlier_failure(self, service):
        create(service)
        record(service, UNITS[0], "failed", "job1")

        result = record(service, UNITS[0], "passed", "job2")

        assert result.campaign.counts.passed == 1
        assert result.campaign.counts.failed == 0

    def test_an_unplanned_unit_is_rejected(self, service, store):
        create(service)
        before = store.replay("1234567")
        stranger = Unit("features/z.feature", "Z", "focal", "lxd-vm")

        with pytest.raises(CampaignError) as error:
            record(service, stranger, "failed", "job1")

        assert "unplanned units" in str(error.value)
        assert store.replay("1234567") == before

    def test_an_unknown_install_source_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError) as error:
            record(service, UNITS[0], "passed", "job1", install_from="nope")

        assert "install source" in str(error.value)

    def test_an_empty_payload_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.record_attempts(
                campaign_id="1234567", payload=[], install_from="proposed"
            )

    def test_mcp_results_are_classified(self, service):
        create(service)
        unit = UNITS[0]

        result = service.record_attempts(
            campaign_id="1234567",
            payload=[
                {
                    "unit": {
                        "feature": unit.feature,
                        "scenario": unit.scenario,
                        "release": unit.release,
                        "machine_type": unit.machine_type,
                    },
                    "result": {
                        "status": "completed",
                        "ok": True,
                        "job_id": "job-abc",
                        "summary": {
                            "scenarios": {"passed": 1},
                            "features": {"passed": 1},
                        },
                    },
                }
            ],
            install_from="proposed",
            from_mcp=True,
        )

        assert result.campaign.counts.passed == 1


class TestNextUnits:
    def test_unattempted_units_come_before_problems(self, service):
        create(service)
        record(service, UNITS[0], "failed", "job1")

        result = service.next_units(campaign_id="1234567", limit=5)

        assert [u.state for u in result.units] == [
            "unattempted",
            "unattempted",
            "failed",
        ]

    def test_a_passing_unit_is_never_returned(self, service):
        create(service)
        record(service, UNITS[0], "passed", "job1")

        result = service.next_units(campaign_id="1234567", limit=5)

        assert UNITS[0] not in [
            Unit(u.feature, u.scenario, u.release, u.machine_type)
            for u in result.units
        ]

    def test_the_limit_caps_the_selection(self, service):
        create(service)

        assert (
            len(service.next_units(campaign_id="1234567", limit=2).units) == 2
        )

    def test_filters_narrow_the_selection(self, service):
        create(service)

        result = service.next_units(
            campaign_id="1234567", filters=Filters(release=("noble",)), limit=5
        )

        assert [u.release for u in result.units] == ["noble"]


class TestUnitHistory:
    def test_every_attempt_is_listed_oldest_first(self, service):
        create(service)
        record(service, UNITS[0], "failed", "job1")
        record(service, UNITS[0], "passed", "job2")

        result = service.unit_history(
            campaign_id="1234567", filters=Filters(release=("jammy",))
        )
        attempts = result.units[0].attempts

        # Two tries, so two attempts.
        assert [(a.outcome, a.job_id) for a in attempts] == [
            ("failed", "job1"),
            ("passed", "job2"),
        ]
        assert all(a.install_from == "proposed" for a in attempts)

    def test_units_without_attempts_are_still_listed(self, service):
        create(service)

        result = service.unit_history(campaign_id="1234567")

        assert len(result.units) == 3
        assert result.units[0].attempts == []

    def test_the_list_is_capped_and_says_so(self, service):
        create(service)

        result = service.unit_history(campaign_id="1234567", limit=2)

        assert len(result.units) == 2
        assert result.truncated

    def test_an_oversized_limit_is_clamped(self, service):
        create(service)

        result = service.unit_history(campaign_id="1234567", limit=10_000)

        assert result.limit_clamped


class TestAwaitEvents:
    def test_creating_a_campaign_announces_it(self, service):
        create(service, install_from="proposed", max_lanes=4)

        result = service.await_events(campaign_id="1234567")

        assert [e.kind for e in result.events] == ["campaign.created"]
        assert result.events[0].data["total_units"] == 3
        assert result.events[0].data["install_from"] == "proposed"
        assert result.events[0].data["max_lanes"] == 4

    def test_the_cursor_advances_past_what_was_read(self, service):
        create(service)

        first = service.await_events(campaign_id="1234567")
        again = service.await_events(
            campaign_id="1234567", since_seq=first.next_seq
        )

        assert first.next_seq == 1
        assert again.events == []
        assert again.next_seq == 1

    def test_it_always_reports_where_the_campaign_stands(self, service):
        create(service)

        # Read past everything, so the batch is empty on purpose.
        result = service.await_events(campaign_id="1234567", since_seq=99)

        assert result.events == []
        assert result.timed_out
        assert result.campaign.counts.unattempted == 3
        assert result.lifecycle == "created"
        assert result.lanes_busy == 0

    def test_a_filter_selects_kinds(self, service):
        create(service)

        result = service.await_events(campaign_id="1234567", kinds=["unit.*"])

        assert result.events == []
        assert result.latest_seq == 1

    def test_an_unknown_kind_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.await_events(campaign_id="1234567", kinds=["nope.*"])

    def test_a_non_positive_limit_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.await_events(campaign_id="1234567", limit=0)
