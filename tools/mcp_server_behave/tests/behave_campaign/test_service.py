import pytest

from behave_campaign.adapters import JsonlCampaignStore, JsonlEventLog
from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    Filters,
    Lifecycle,
    LifecycleRecord,
    NewEvent,
    RepoState,
    Unit,
)
from behave_campaign.service import CampaignService
from behave_mcp.pools import Pool

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
        lane_ceiling={Pool.LXD: 8, Pool.CLOUD: 2, Pool.WSL: 1},
    )


MIXED_UNITS = [
    *UNITS,
    Unit("features/c.feature", "C", "jammy", "aws.generic"),
    Unit("features/d.feature", "D", "noble", "wsl"),
]


def _service_over(
    tmp_path,
    events,
    units,
    ceiling={Pool.LXD: 8, Pool.CLOUD: 2, Pool.WSL: 1},
):
    return CampaignService(
        store=JsonlCampaignStore(tmp_path / "campaigns"),
        features=FakeFeatureReader(units),
        events=events,
        now=lambda: AT,
        repo_state=lambda root: REPO,
        lane_ceiling=ceiling,
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
        assert result.state.counts.unattempted == 3
        assert result.state.counts.passed == 0

    def test_it_records_how_the_campaign_was_built(self, service):
        result = create(
            service,
            install_from="proposed",
            lanes={Pool.LXD: 8},
            releases=["jammy"],
        )

        assert result.campaign.campaign_id == "1234567"
        assert result.campaign.created_at == AT
        assert result.campaign.install_from == "proposed"
        assert result.campaign.lanes == {"lxd": 8}
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
            create(service, lanes={Pool.LXD: 9})

        assert "lxd job limit of 8" in str(error.value)

    def test_the_server_limit_itself_is_allowed(self, service):
        result = create(service, lanes={Pool.LXD: 8})

        assert result.campaign.lanes == {"lxd": 8}

    def test_a_pool_left_out_gets_one_lane(self, service):
        assert create(service).campaign.lanes == {"lxd": 1}

    def test_lanes_are_kept_for_each_pool_in_scope(self, tmp_path, events):
        service = _service_over(tmp_path, events, MIXED_UNITS)

        result = create(service, lanes={Pool.LXD: 6, Pool.CLOUD: 2})

        assert result.campaign.lanes == {"lxd": 6, "cloud": 2, "wsl": 1}

    def test_zero_lanes_is_rejected(self, service):
        with pytest.raises(CampaignError, match="lxd lanes must be"):
            create(service, lanes={Pool.LXD: 0})

    def test_lanes_for_a_pool_out_of_scope_are_rejected(self, service):
        with pytest.raises(CampaignError, match="no unit in scope"):
            create(service, lanes={Pool.WSL: 1})

    def test_more_than_one_wsl_lane_is_rejected(self, tmp_path, events):
        service = _service_over(tmp_path, events, MIXED_UNITS)

        with pytest.raises(CampaignError, match="wsl lanes may not exceed"):
            create(service, lanes={Pool.WSL: 2})

    def test_a_pool_the_server_disabled_is_rejected(self, tmp_path, events):
        service = _service_over(
            tmp_path, events, MIXED_UNITS, ceiling={Pool.LXD: 8, Pool.WSL: 1}
        )

        with pytest.raises(CampaignError, match="disabled the cloud pool"):
            create(service)

    def test_without_a_ceiling_any_lane_count_is_allowed(
        self, tmp_path, events
    ):
        service = _service_over(tmp_path, events, UNITS, ceiling=None)

        result = create(service, lanes={Pool.LXD: 64})

        assert result.campaign.lanes == {"lxd": 64}


class TestListUnits:
    def test_every_selected_unit_with_its_state(self, service, store):
        create(service)
        store.append(
            "1234567",
            [
                AttemptFinished(
                    unit=UNITS[0], job_id="job1", outcome="failed", at=AT
                )
            ],
        )

        listed = service.list_units(campaign_id="1234567")

        assert [(u.release, u.state, u.job_id) for u in listed.units] == [
            ("jammy", "failed", "job1"),
            ("noble", "unattempted", None),
            ("jammy", "unattempted", None),
        ]
        assert not listed.truncated

    def test_the_list_is_capped_and_says_so(self, service):
        create(service)

        listed = service.list_units(campaign_id="1234567", limit=2)

        assert len(listed.units) == 2
        assert listed.truncated

    def test_an_oversized_limit_is_clamped(self, service):
        create(service)

        assert service.list_units(
            campaign_id="1234567", limit=10_000
        ).limit_clamped

    def test_a_non_positive_limit_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError):
            service.list_units(campaign_id="1234567", limit=0)


class TestListCampaigns:
    def test_it_summarises_each_stored_campaign(self, service):
        create(service, campaign_id="111")
        create(service, campaign_id="222", releases=["jammy"])

        result = service.list_campaigns()

        assert [c.campaign_id for c in result.campaigns] == ["111", "222"]
        assert [c.total_units for c in result.campaigns] == [3, 2]
        assert [c.lifecycle for c in result.campaigns] == ["created"] * 2
        # A row says how wide the scope is, not what is in it.
        assert [c.scope_size.releases for c in result.campaigns] == [0, 1]
        assert not hasattr(result.campaigns[0], "scope")

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

        assert status.state.counts.unattempted == 3
        assert status.running == []
        assert status.problems == []

    def test_status_never_lists_individual_units(self, service):
        create(service)

        status = service.campaign_status(campaign_id="1234567")

        assert not hasattr(status, "units")

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
        assert status.state.counts.running == 1
        assert status.state.counts.failed == 1
        assert status.state.counts.unattempted == 1
        assert status.problem_scenarios is None

    def test_it_counts_busy_lanes_in_each_pool(self, tmp_path, events, store):
        service = _service_over(tmp_path, events, MIXED_UNITS)
        create(service)
        store.append(
            "1234567",
            [
                AttemptStarted(
                    unit=MIXED_UNITS[-1],
                    job_id="job1",
                    install_from="proposed",
                    at=AT,
                ),
            ],
        )

        status = service.campaign_status(campaign_id="1234567")

        assert status.lanes_busy_by_pool == {"lxd": 0, "cloud": 0, "wsl": 1}

    def test_problems_can_be_grouped_by_scenario(self, service, store):
        create(service)
        store.append(
            "1234567",
            [
                AttemptFinished(
                    unit=UNITS[0], job_id="job1", outcome="failed", at=AT
                ),
                AttemptFinished(
                    unit=UNITS[1], job_id="job2", outcome="error", at=AT
                ),
                AttemptFinished(
                    unit=UNITS[2], job_id="job3", outcome="passed", at=AT
                ),
            ],
        )

        status = service.campaign_status(
            campaign_id="1234567", group_by="scenario"
        )

        assert status.problems is None
        (row,) = status.problem_scenarios or []
        assert (row.feature, row.scenario) == ("features/a.feature", "A")
        # Units are the catalog's own combos, plus where to look.
        assert [u.model_dump() for u in row.failed] == [
            {
                "release": "jammy",
                "machine_type": "lxd-container",
                "job_id": "job1",
                "attempt_count": 1,
            }
        ]
        assert [(u.release, u.machine_type) for u in row.error] == [
            ("noble", "lxd-vm")
        ]
        assert row.skipped == []

    def test_problems_are_capped_but_counted(self, service, store):
        create(service)
        store.append(
            "1234567",
            [
                AttemptFinished(
                    unit=unit, job_id="job%d" % i, outcome="failed", at=AT
                )
                for i, unit in enumerate(UNITS)
            ],
        )

        status = service.campaign_status(
            campaign_id="1234567", problems_limit=2
        )
        grouped = service.campaign_status(
            campaign_id="1234567", problems_limit=1, group_by="scenario"
        )

        assert len(status.problems or []) == 2
        assert status.problems_total == 3
        # Grouped, the cap is on scenario rows; the total is still units.
        assert len(grouped.problem_scenarios or []) == 1
        assert grouped.problems_total == 3

    def test_an_unknown_grouping_is_rejected(self, service):
        create(service)

        with pytest.raises(CampaignError, match="group_by"):
            service.campaign_status(campaign_id="1234567", group_by="feature")

    def test_filters_narrow_the_counts(self, service):
        create(service)

        status = service.campaign_status(
            campaign_id="1234567", filters=Filters(release=("noble",))
        )

        assert status.state.counts.unattempted == 1

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

        listed = service.list_units(
            campaign_id="1234567", filters=Filters(state=("passed",))
        )

        assert [u.state for u in listed.units] == ["passed"]

    def test_it_reports_the_stored_header(self, service):
        create(service, install_from="proposed", lanes={Pool.LXD: 4})

        status = service.campaign_status(campaign_id="1234567")

        assert status.campaign.install_from == "proposed"
        assert status.campaign.lanes == {"lxd": 4}
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
        assert result.counts.failed == 1
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

        assert result.counts.passed == 1
        assert result.counts.failed == 0

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

        assert result.counts.passed == 1


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
    def test_failure_messages_can_be_shortened_on_read(self, service, events):
        create(service)
        events.append(
            "1234567",
            [
                NewEvent(
                    kind="unit.failed",
                    at=AT,
                    data={
                        "failures": [
                            {
                                "step": "s",
                                "status": "failed",
                                "error_message": "x" * 50,
                            }
                        ]
                    },
                )
            ],
        )

        short = service.await_events(
            campaign_id="1234567", kinds=["unit.failed"], failure_chars=5
        )
        whole = service.await_events(
            campaign_id="1234567", kinds=["unit.failed"]
        )

        assert short.events[0].data["failures"][0]["error_message"] == "xxxxx"
        assert len(whole.events[0].data["failures"][0]["error_message"]) == 50
        assert short.events[0].data["failures"][0]["step"] == "s"

    def test_creating_a_campaign_announces_it(self, service):
        create(service, install_from="proposed", lanes={Pool.LXD: 4})

        result = service.await_events(campaign_id="1234567")

        assert [e.kind for e in result.events] == ["campaign.created"]
        assert result.events[0].data["total_units"] == 3
        assert result.events[0].data["install_from"] == "proposed"
        assert result.events[0].data["lanes"] == {"lxd": 4}

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
        assert result.counts.unattempted == 3
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


class FakeJobFiles:
    """Job files by id, all present except those named in ``without``."""

    def __init__(self, tmp_path, without=()):
        self.tmp_path = tmp_path
        self.without = set(without)

    def files(self, job_id):
        found = {}
        for name in ("meta.json", "report.json", "stdout.log"):
            if (job_id, name) in self.without:
                continue
            path = self.tmp_path / "jobs" / job_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            found[name] = path
        return found


class FakeBundleWriter:
    """Keeps what would have been written, by bundle name."""

    def __init__(self):
        self.output = None
        self.top = None
        self.entries = {}

    def write(self, output, top, entries):
        self.output, self.top = output, top
        self.entries = {entry.name: entry for entry in entries}
        return []


def finish(store, unit, job_id, outcome):
    store.append(
        "1234567",
        [
            AttemptStarted(
                unit=unit, job_id=job_id, install_from="proposed", at=AT
            ),
            AttemptFinished(unit=unit, job_id=job_id, outcome=outcome, at=AT),
        ],
    )


@pytest.fixture
def completed(service, store):
    """A campaign that ran, retried one unit, and finished."""
    create(service, install_from="proposed")
    store.append("1234567", [LifecycleRecord(state=Lifecycle.RUNNING, at=AT)])
    finish(store, UNITS[0], "a1", "failed")
    finish(store, UNITS[0], "a2", "passed")
    finish(store, UNITS[1], "n1", "passed")
    finish(store, UNITS[2], "b1", "error")
    service.complete_campaign(campaign_id="1234567")
    return "1234567"


def export(service, tmp_path, jobs=None, writer=None):
    writer = writer or FakeBundleWriter()
    response = service.export_campaign(
        campaign_id="1234567",
        output=tmp_path / "out.tar.gz",
        jobs=jobs or FakeJobFiles(tmp_path),
        writer=writer,
    )
    return response, writer


class TestExportCampaign:
    def test_only_a_complete_campaign_exports(self, service, store, tmp_path):
        create(service)
        store.append(
            "1234567", [LifecycleRecord(state=Lifecycle.RUNNING, at=AT)]
        )
        finish(store, UNITS[0], "a1", "passed")

        with pytest.raises(CampaignError, match="2 unattempted"):
            export(service, tmp_path)

    def test_bundles_every_attempt_and_the_raw_records(
        self, service, completed, tmp_path
    ):
        response, writer = export(service, tmp_path)

        assert writer.top == "1234567"
        assert response.jobs == 4
        for job_id in ("a1", "a2", "n1", "b1"):
            for name in ("meta.json", "report.json", "stdout.log"):
                assert "jobs/{}/{}".format(job_id, name) in writer.entries
        assert "audit/campaign.jsonl" in writer.entries
        assert "audit/events.jsonl" in writer.entries
        for name in ("SUMMARY.md", "campaign.json", "units.csv"):
            assert writer.entries[name].data

    def test_summary_shows_each_unit_once_in_its_final_state(
        self, service, completed, tmp_path
    ):
        response, writer = export(service, tmp_path)
        summary = writer.entries["SUMMARY.md"].data.decode()

        assert response.counts.passed == 2
        assert response.counts.error == 1
        assert "## Passed (2)" in summary
        assert "## Not passed (1)" in summary
        assert "a1" not in summary
        assert "(error, job b1)" in summary

    def test_missing_job_files_are_reported(
        self, service, completed, tmp_path
    ):
        jobs = FakeJobFiles(tmp_path, without=[("b1", "report.json")])

        response, writer = export(service, tmp_path, jobs=jobs)

        assert response.missing_files == ["jobs/b1/report.json"]
        assert "jobs/b1/report.json" not in writer.entries
        summary = writer.entries["SUMMARY.md"].data.decode()
        assert "(error, job b1, files missing)" in summary


class TestCompleteCampaign:
    def test_marks_a_campaign_with_no_work_left_complete(
        self, service, store, events
    ):
        create(service)
        for number, unit in enumerate(UNITS):
            finish(store, unit, "j{}".format(number), "failed")

        state = service.complete_campaign(campaign_id="1234567")

        assert state.lifecycle == "complete"
        assert store.replay("1234567")[-1] == LifecycleRecord(
            state=Lifecycle.COMPLETE, at=AT
        )
        done = events.read(
            "1234567", since_seq=0, kinds=["campaign.complete"], limit=10
        )
        assert done[0].data["counts"]["failed"] == 3

    def test_refuses_while_units_are_left(self, service, store):
        create(service)
        finish(store, UNITS[0], "j1", "passed")

        with pytest.raises(CampaignError, match="2 unit"):
            service.complete_campaign(campaign_id="1234567")

        assert "complete" not in [
            getattr(r, "state", None) for r in store.replay("1234567")
        ]

    def test_refuses_a_cancelled_campaign(self, service, store):
        create(service)
        store.append(
            "1234567", [LifecycleRecord(state=Lifecycle.CANCELLED, at=AT)]
        )

        with pytest.raises(CampaignError, match="reopen"):
            service.complete_campaign(campaign_id="1234567")

    def test_completing_twice_writes_once(self, service, store):
        create(service)
        for number, unit in enumerate(UNITS):
            finish(store, unit, "j{}".format(number), "passed")

        service.complete_campaign(campaign_id="1234567")
        service.complete_campaign(campaign_id="1234567")

        written = [
            r
            for r in store.replay("1234567")
            if isinstance(r, LifecycleRecord)
        ]
        assert len(written) == 1
