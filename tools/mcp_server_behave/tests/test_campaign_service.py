import pytest

from behave_campaign.adapters import JsonlCampaignStore
from behave_campaign.domain import (
    AttemptRecord,
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
        return {"releases": [], "machine_types": []}


@pytest.fixture
def features():
    return FakeFeatureReader()


@pytest.fixture
def service(tmp_path, features):
    return CampaignService(
        store=JsonlCampaignStore(tmp_path / "campaigns"),
        features=features,
        now=lambda: AT,
        repo_state=lambda root: REPO,
        max_parallel_jobs=8,
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
            features=["features/a.feature"],
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

    def test_include_units_returns_them(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", include_units=True
        )

        assert len(status.units) == 3
        assert not status.truncated

    def test_the_unit_list_is_capped_and_says_so(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", include_units=True, limit=2
        )

        assert len(status.units) == 2
        assert status.truncated

    def test_an_oversized_limit_is_clamped(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", include_units=True, limit=10_000
        )

        assert status.limit_clamped

    def test_a_non_positive_limit_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.campaign_status(campaign_id="1234567", limit=0)

    def test_running_and_problem_units_are_always_reported(
        self, service, store
    ):
        create(service)
        store.append(
            "1234567",
            [
                AttemptRecord(
                    unit=UNITS[0],
                    state="running",
                    job_id="job1",
                    install_from="proposed",
                    at=AT,
                ),
                AttemptRecord(
                    unit=UNITS[1],
                    state="failed",
                    job_id="job2",
                    install_from="proposed",
                    at=AT,
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
                AttemptRecord(
                    unit=UNITS[0],
                    state="passed",
                    job_id="job1",
                    install_from="proposed",
                    at=AT,
                )
            ],
        )

        status = service.campaign_status(
            campaign_id="1234567",
            filters=Filters(state=("passed",)),
            include_units=True,
        )

        assert [u.state for u in status.units] == ["passed"]

    def test_it_reports_the_stored_header(self, service):
        create(service, install_from="proposed", max_lanes=4)

        status = service.campaign_status(campaign_id="1234567")

        assert status.campaign.install_from == "proposed"
        assert status.campaign.max_lanes == 4
        assert status.campaign.created_at == AT
