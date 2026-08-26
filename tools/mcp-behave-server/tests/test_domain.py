"""Plain unit tests for pure domain logic."""

import json

from behave_mcp import domain


def test_classify_job_status_live_handle_running():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "live_handle_running"


def test_classify_job_status_live_handle_exited_ok():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=0,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "live_handle_exited"


def test_classify_job_status_live_handle_exited_failed():
    result = domain.classify_job_status(
        has_live_handle=True,
        returncode=1,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "live_handle_exited"


def test_classify_job_status_recovered_report_present_ok():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=True,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "report_present"


def test_classify_job_status_recovered_report_present_failed():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=False,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "report_present"


def test_classify_job_status_recovered_pid_alive_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "pid_alive_no_report"


def test_classify_job_status_recovered_pid_dead_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_dead_no_report"


def test_classify_job_status_recovered_pid_unknown_no_report():
    result = domain.classify_job_status(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=None,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_unknown_no_report"


# ---- job_matches_result_filters ----


def test_job_matches_result_filters_no_filters_matches_everything():
    assert domain.job_matches_result_filters(
        {},
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_job_ids():
    metadata: dict = {}
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids={"job1", "job2"},
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job3",
        job_ids={"job1", "job2"},
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_feature_file():
    metadata = {"feature_file": "features/cli/attach.feature"}
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file="features/cli/attach.feature",
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file="features/cli/other.feature",
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_scenario_name_substring():
    metadata = {"scenario_name": "Attach invalid token"}
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name="invalid",
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name="expired",
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_release_and_machine_type():
    metadata = {"releases": ["jammy"], "machine_types": ["lxd-container"]}
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="jammy",
        machine_type="lxd-container",
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="resolute",
        machine_type=None,
    )


# ---- resolve_scenario_combo ----


def test_resolve_scenario_combo_matches_known_location():
    combo_map = {
        "features/_version.feature:27": {
            "release": "xenial",
            "machine_type": "lxd-container",
        }
    }
    release, machine_type, precise = domain.resolve_scenario_combo(
        "features/_version.feature:27", combo_map
    )
    assert (release, machine_type, precise) == (
        "xenial",
        "lxd-container",
        True,
    )


def test_resolve_scenario_combo_unresolved_when_location_unknown():
    combo_map = {
        "features/_version.feature:27": {
            "release": "xenial",
            "machine_type": "lxd-container",
        }
    }
    assert domain.resolve_scenario_combo(
        "features/_version.feature:99", combo_map
    ) == (None, None, False)


def test_resolve_scenario_combo_unresolved_when_no_report():
    assert domain.resolve_scenario_combo(
        "features/_version.feature:27", {}
    ) == (None, None, False)


# ---- parse_combo_report ----


def test_parse_combo_report_builds_location_map():
    lines = [
        json.dumps(
            {
                "location": "features/f.feature:10",
                "status": "passed",
                "release": "jammy",
                "machine_type": "lxd-container",
            }
        ),
        json.dumps(
            {
                "location": "features/f.feature:11",
                "status": "skipped",
                "release": "resolute",
                "machine_type": "lxd-vm",
            }
        ),
    ]

    combo_map = domain.parse_combo_report(lines)

    assert combo_map == {
        "features/f.feature:10": {
            "release": "jammy",
            "machine_type": "lxd-container",
        },
        "features/f.feature:11": {
            "release": "resolute",
            "machine_type": "lxd-vm",
        },
    }


def test_parse_combo_report_skips_malformed_and_incomplete_lines():
    lines = [
        "not json",
        "",
        json.dumps({"location": "features/f.feature:10"}),  # missing combo
        json.dumps(
            {
                "location": "features/f.feature:11",
                "release": "jammy",
                "machine_type": "lxd-container",
            }
        ),
    ]

    assert domain.parse_combo_report(lines) == {
        "features/f.feature:11": {
            "release": "jammy",
            "machine_type": "lxd-container",
        }
    }


# ---- scenario_status_from_element ----


def test_scenario_status_from_element_prefers_reported_status():
    scenario = {
        "status": "skipped",
        "steps": [],
    }
    assert domain.scenario_status_from_element(scenario) == "skipped"


def test_scenario_status_from_element_maps_error_statuses_to_failed():
    for reported_status in ("failed", "error", "undefined", "hook_error"):
        scenario = {"status": reported_status, "steps": []}
        assert domain.scenario_status_from_element(scenario) == "failed"


def test_scenario_status_from_element_falls_back_to_steps_when_absent():
    scenario = {
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert domain.scenario_status_from_element(scenario) == "passed"


def test_scenario_status_from_element_falls_back_for_unknown_reported_status():
    scenario = {
        "status": "some-future-behave-status",
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert domain.scenario_status_from_element(scenario) == "passed"


# ---- summarize_report ----


def test_summarize_report_classifies_skipped_scenarios_correctly():
    report_data = [
        {
            "name": "feature",
            "elements": [
                {
                    "name": "passing scenario",
                    "status": "passed",
                    "steps": [
                        {"name": "step", "result": {"status": "passed"}}
                    ],
                },
                {
                    "name": "skipped scenario",
                    "status": "skipped",
                    "steps": [],
                },
            ],
        }
    ]

    result = domain.summarize_report(report_data)

    assert result.summary["scenarios"]["passed"] == 1
    assert result.summary["scenarios"]["skipped"] == 1
    assert result.summary["scenarios"]["unknown"] == 0


# ---- combo_group_counts / merge_combo_group_counts / grouped_counts ----


def _scenario_element(location, status, name="scenario"):
    return {
        "name": name,
        "location": location,
        "steps": [{"name": "step", "result": {"status": status}}],
    }


def test_combo_group_counts_classifies_skipped_scenarios_correctly():
    """A skipped scenario has no executed steps, only a scenario-level

    ``status`` -- regression test for the bug where such scenarios were
    mis-bucketed as "unknown" instead of "skipped".
    """
    combo_map = {
        "features/f.feature:10": {
            "release": "jammy",
            "machine_type": "lxd-container",
        },
    }
    report_data = [
        {
            "name": "feature",
            "elements": [
                {
                    "name": "scenario",
                    "location": "features/f.feature:10",
                    "status": "skipped",
                    "steps": [],
                },
            ],
        }
    ]

    by_release, _ = domain.combo_group_counts(report_data, combo_map, [], [])

    assert by_release["jammy"]["skipped"] == 1
    assert by_release["jammy"]["unknown"] == 0


def test_combo_group_counts_precise_from_combo_map():
    combo_map = {
        "features/f.feature:10": {
            "release": "jammy",
            "machine_type": "lxd-container",
        },
        "features/f.feature:11": {
            "release": "resolute",
            "machine_type": "lxd-vm",
        },
    }
    report_data = [
        {
            "name": "feature",
            "elements": [
                _scenario_element("features/f.feature:10", "passed"),
                _scenario_element("features/f.feature:11", "failed"),
            ],
        }
    ]

    by_release, by_machine_type = domain.combo_group_counts(
        report_data, combo_map, [], []
    )

    assert by_release["jammy"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
        "precise": True,
    }
    assert by_release["resolute"]["failed"] == 1
    assert by_machine_type["lxd-container"]["passed"] == 1
    assert by_machine_type["lxd-vm"]["failed"] == 1


def test_combo_group_counts_falls_back_to_declared_lists_when_unresolved():
    report_data = [
        {
            "name": "feature",
            "elements": [
                _scenario_element("features/f.feature:10", "passed"),
            ],
        }
    ]

    by_release, by_machine_type = domain.combo_group_counts(
        report_data, {}, ["jammy", "noble"], ["lxd-container"]
    )

    assert by_release["jammy"]["precise"] is False
    assert by_release["noble"]["precise"] is False
    assert by_release["jammy"]["passed"] == 1
    assert by_machine_type["lxd-container"]["passed"] == 1


def test_merge_combo_group_counts_sums_and_ands_precise():
    target: dict = {}
    domain.merge_combo_group_counts(
        target,
        {
            "jammy": {
                "total": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "unknown": 0,
                "precise": True,
            }
        },
    )
    domain.merge_combo_group_counts(
        target,
        {
            "jammy": {
                "total": 1,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "unknown": 0,
                "precise": False,
            }
        },
    )

    assert target["jammy"]["total"] == 2
    assert target["jammy"]["passed"] == 1
    assert target["jammy"]["failed"] == 1
    assert target["jammy"]["precise"] is False


def test_grouped_counts_from_dict_sorted_by_name():
    counts = {
        "resolute": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
            "precise": True,
        },
        "jammy": {
            "total": 2,
            "passed": 1,
            "failed": 1,
            "skipped": 0,
            "unknown": 0,
            "precise": False,
        },
    }

    result = domain.grouped_counts_from_dict(counts)

    assert [g.name for g in result] == ["jammy", "resolute"]
    assert result[0].precise is False
    assert result[1].total == 1


# ---- combo_failures_from_report ----


def test_combo_failures_from_report_precise_combo():
    combo_map = {
        "features/f.feature:10": {
            "release": "jammy",
            "machine_type": "lxd-container",
        }
    }
    report_data = [
        {
            "name": "my-feature",
            "elements": [
                {
                    "name": "my-scenario",
                    "location": "features/f.feature:10",
                    "steps": [
                        {
                            "name": "a failing step",
                            "result": {
                                "status": "failed",
                                "error_message": "boom",
                            },
                        }
                    ],
                }
            ],
        }
    ]

    failures = domain.combo_failures_from_report(
        report_data, "job1", combo_map, [], []
    )

    assert len(failures) == 1
    failure = failures[0]
    assert failure.feature == "my-feature"
    assert failure.scenario == "my-scenario"
    assert failure.step == "a failing step"
    assert failure.job_id == "job1"
    assert failure.releases == ["jammy"]
    assert failure.machine_types == ["lxd-container"]
    assert failure.precise is True


def test_combo_failures_from_report_falls_back_to_declared_lists():
    report_data = [
        {
            "name": "my-feature",
            "elements": [
                {
                    "name": "my-scenario",
                    "location": "features/f.feature:10",
                    "steps": [
                        {
                            "name": "a failing step",
                            "result": {
                                "status": "failed",
                                "error_message": "boom",
                            },
                        }
                    ],
                }
            ],
        }
    ]

    failures = domain.combo_failures_from_report(
        report_data, "job1", {}, ["jammy"], ["lxd-container", "lxd-vm"]
    )

    assert len(failures) == 1
    failure = failures[0]
    assert failure.releases == ["jammy"]
    assert failure.machine_types == ["lxd-container", "lxd-vm"]
    assert failure.precise is False
