import copy
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from coverage_gaps import (  # noqa: E402
    Finding,
    GapStatus,
    ScenarioCoverage,
    aggregate_scenarios,
    find_gaps,
    scenario_coverage_from_feature_detail,
)
from release_catalog import ReleaseCatalog  # noqa: E402

from features import behave_features  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "release_coverage"

# Fixtures below are real describe_feature-shaped data, pulled read-only via
# the behave MCP against the actual .feature files (never edited). `tags`
# in each fixture file is exactly what's really there today -- empty, for
# every one of them, since no scenario carries @releases.* tags yet. Tests
# add tags in-memory (see `_with_tags`) to exercise the parser/evaluator
# against what a scenario *should* eventually carry, without ever writing
# back to the fixture file or the real .feature file.

TODAY = date(2026, 8, 1)

# A small, chronologically-ordered sample mirroring ubuntu.csv rows -- same
# shape as tools/tests/test_release_catalog.py's ROWS, with an eol-legacy
# date added to xenial so the `legacy` tier has a real release to exercise
# against legacy.feature. All status math below is relative to TODAY.
ROWS = [
    {
        "series": "xenial",
        "version": "16.04 LTS",
        "eol": "2021-04-30",
        "eol-esm": "2026-04-23",  # already past TODAY -> not esm
        "eol-legacy": "2031-04-30",  # -> legacy at TODAY
    },
    {
        "series": "bionic",
        "version": "18.04 LTS",
        "eol": "2023-05-31",
        "eol-esm": "2028-04-26",
    },
    {
        "series": "focal",
        "version": "20.04 LTS",
        "eol": "2025-05-29",
        "eol-esm": "2030-04-23",
    },
    {"series": "jammy", "version": "22.04 LTS", "eol": "2027-06-01"},
    {"series": "noble", "version": "24.04 LTS", "eol": "2029-05-31"},
    {
        "series": "questing",
        "version": "25.10",
        "eol": "2026-07-09",
    },  # before TODAY -> eol
    {"series": "resolute", "version": "26.04 LTS", "eol": "2031-05-29"},
    {
        "series": "stonking",
        "version": "26.10",
        "release": "2026-10-15",
        "eol": "2027-07-15",
    },
]
# Resulting statuses at TODAY: xenial=legacy, bionic=esm, focal=esm,
# jammy=supported, noble=supported, questing=eol, resolute=supported,
# stonking=devel.


@pytest.fixture
def catalog():
    return ReleaseCatalog.from_rows(ROWS, today=TODAY)


def _feature_detail_from_payload(
    payload: dict,
) -> behave_features.FeatureDetail:
    """Fixtures are JSON dicts, MCP describe_feature-shaped. Convert
    through the same behave_features.FeatureDetail path production code
    uses, rather than duplicating ScenarioCoverage-construction logic here.
    """
    return behave_features.FeatureDetail(
        path=payload["feature_file"],
        title=payload.get("title", ""),
        tags=list(payload.get("tags", [])),
        requires_config=list(payload.get("requires_config", [])),
        scenarios=[
            behave_features.ScenarioSummary(
                name=scenario["name"],
                type=scenario.get("type", "scenario"),
                tags=list(scenario.get("tags", [])),
                requires_config=list(scenario.get("requires_config", [])),
                example_columns=list(scenario.get("example_columns", [])),
                combos=[
                    behave_features.Combo(
                        release=combo["release"],
                        machine_type=combo["machine_type"],
                    )
                    for combo in scenario.get("combos", [])
                ],
            )
            for scenario in payload.get("scenarios", [])
        ],
    )


def load_scenario_coverage(payload: dict):
    return scenario_coverage_from_feature_detail(
        _feature_detail_from_payload(payload)
    )


def _load(fixture_name):
    with open(FIXTURES_DIR / fixture_name, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload, load_scenario_coverage(payload)


def _with_tags(payload, tags_by_scenario_name):
    """Deep-copy `payload` and add `@releases.*` tags to every scenario
    node whose name is a key in `tags_by_scenario_name` -- the hand-authored
    'what this scenario should eventually carry' layer, applied uniformly
    across every aggregated node sharing that name. Never mutates the raw
    fixture.
    """
    annotated = copy.deepcopy(payload)
    for scenario in annotated["scenarios"]:
        extra = tags_by_scenario_name.get(scenario["name"])
        if extra:
            scenario["tags"] = list(scenario.get("tags", [])) + extra
    return annotated


def _scenario(
    feature_file="features/synthetic.feature",
    name="Synthetic scenario",
    combos=None,
    tags=None,
    is_outline=True,
    example_columns=None,
):
    return ScenarioCoverage(
        feature_file=feature_file,
        scenario_name=name,
        is_outline=is_outline,
        example_columns=(
            example_columns
            if example_columns is not None
            else ["release", "machine_type"]
        ),
        combos=set(combos or []),
        tags=list(tags or []),
    )


# ---------------------------------------------------------------------------
# Real, currently-untagged data: every one of these must come back
# UNCLASSIFIED, never GAP -- the key behavior distinguishing "no signal yet"
# from "known missing."
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture_name",
    [
        "anbox_container.json",
        "anbox_vm.json",
        "fix_unattached.json",
        "fix_lifecycle.json",
        "daemon_interim.json",
        "legacy_status.json",
        "version_full.json",
        "legacy_full.json",
    ],
)
def test_real_untagged_data_is_always_unclassified(catalog, fixture_name):
    _, scenarios = _load(fixture_name)
    findings = find_gaps(catalog, scenarios, today=TODAY)
    assert findings, "expected at least one finding"
    assert all(f.status == GapStatus.UNCLASSIFIED for f in findings)


# ---------------------------------------------------------------------------
# Real scenarios (features/anbox.feature), hand-annotated with the tags
# they should eventually carry.
# ---------------------------------------------------------------------------
class TestAnboxContainer:
    def test_annotated_is_fully_covered(self, catalog):
        payload, _ = _load("anbox_container.json")
        annotated = _with_tags(
            payload,
            {
                "Enable Anbox cloud service in a container": [
                    "@releases.lts.supported"
                ]
            },
        )
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert findings == []


class TestAnboxVm:
    def test_annotated_flags_the_real_missing_resolute_row(self, catalog):
        # features/anbox.feature really does have this gap today: the VM
        # variant only has jammy/noble rows, unlike the container variant.
        payload, _ = _load("anbox_vm.json")
        annotated = _with_tags(
            payload,
            {
                "Enable Anbox cloud service in a VM": [
                    "@releases.lts.supported"
                ]
            },
        )
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert len(findings) == 1
        assert findings[0] == Finding(
            "features/anbox.feature",
            "Enable Anbox cloud service in a VM",
            GapStatus.GAP,
            release="resolute",
            machine_type="lxd-vm",
            bucket="lts.supported",
        )


# ---------------------------------------------------------------------------
# Real precondition-split scenario (features/fix.feature): three
# Scenario Outline nodes sharing one name, split by contract_token
# requirement.
# ---------------------------------------------------------------------------
class TestFixUnattachedAggregation:
    def test_raw_aggregation_unions_combos_across_nodes(self):
        _, scenarios = _load("fix_unattached.json")
        aggregated = aggregate_scenarios(scenarios)
        assert len(aggregated) == 1
        combined = aggregated[0]
        assert combined.tag_conflict_detail is None
        assert combined.combos == {
            ("focal", "lxd-container"),
            ("focal", "wsl"),
            ("xenial", "lxd-container"),
            ("xenial", "lxd-vm"),
            ("bionic", "lxd-container"),
            ("bionic", "wsl"),
        }

    def test_annotated_identical_tags_across_nodes_flags_real_gaps(
        self, catalog
    ):
        payload, _ = _load("fix_unattached.json")
        annotated = _with_tags(
            payload,
            {
                "Fix command on an unattached machine": [
                    "@releases.lts.supported"
                ]
            },
        )
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert len(findings) == 9  # 3 missing releases x 3 known machine_types
        assert {f.release for f in findings} == {"jammy", "noble", "resolute"}
        assert all(f.status == GapStatus.GAP for f in findings)

    def test_conflicting_tags_across_nodes_is_a_tag_error(self, catalog):
        # Identical names within one file MUST carry identical @releases.*
        # tags (dev-docs/reference/release_coverage_tags.md) -- a mismatch
        # is a data hygiene bug, flagged rather than silently resolved.
        payload, _ = _load("fix_unattached.json")
        annotated = copy.deepcopy(payload)
        annotated["scenarios"][0]["tags"] = ["@releases.lts.supported"]
        annotated["scenarios"][1]["tags"] = ["@releases.lts.esm"]
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert len(findings) == 1
        assert findings[0].status == GapStatus.TAG_ERROR
        assert "different @releases.* tags" in findings[0].detail


class TestFixLifecycle:
    def test_annotated_matches_worked_example_3(self, catalog):
        # dev-docs/explanation/release_coverage_model.md worked example #3:
        # tracks={lts:{supported,esm}}, covers only bionic today.
        # Missing = {focal, jammy, noble, resolute} -- not just focal.
        payload, _ = _load("fix_lifecycle.json")
        name = "Fix command on a machine without security/updates source lists"
        annotated = _with_tags(
            payload, {name: ["@releases.lts.supported", "@releases.lts.esm"]}
        )
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert {f.release for f in findings} == {
            "focal",
            "jammy",
            "noble",
            "resolute",
        }
        assert len(findings) == 12  # 4 releases x 3 known machine_types


class TestDaemonInterimOnly:
    def test_annotated_calendar_gap_produces_no_findings(self, catalog):
        # Worked example #8: no interim release is currently `supported`
        # at TODAY (the gap between questing's expiry and stonking's
        # release) -- correctly zero findings, not an error.
        payload, _ = _load("daemon_interim.json")
        name = "daemon does not start on gcp,azure generic non lts"
        annotated = _with_tags(
            payload, {name: ["@releases.interim.supported"]}
        )
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert findings == []


class TestLegacyStatus:
    def test_annotated_legacy_tier_is_fully_covered(self, catalog):
        payload, _ = _load("legacy_status.json")
        name = "Attached status with legacy contract in a ubuntu machine"
        annotated = _with_tags(payload, {name: ["@releases.lts.legacy"]})
        findings = find_gaps(
            catalog, load_scenario_coverage(annotated), today=TODAY
        )
        assert findings == []


# ---------------------------------------------------------------------------
# Remaining worked examples from the model doc, as standalone (synthetic)
# scenarios -- these mechanisms aren't cleanly demonstrable with the small
# set of real scenarios pulled above.
# ---------------------------------------------------------------------------
class TestWorkedExamplesStandalone:
    def test_until_bound_does_not_exclude_releases_within_it(self, catalog):
        # Worked example #4: until(S, lts)=resolute produces the same R(S)
        # as unbounded today, since resolute is already the newest
        # supported LTS.
        scenario = _scenario(
            combos={("jammy", "lxd-container"), ("noble", "lxd-container")},
            tags=["@releases.lts.supported", "@releases.until.lts.resolute"],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert {(f.release, f.machine_type) for f in findings} == {
            ("resolute", "lxd-container")
        }

    def test_until_bound_excludes_a_release_newer_than_it(self, catalog):
        scenario = _scenario(
            combos={("jammy", "lxd-container")},
            tags=["@releases.lts.supported", "@releases.until.lts.jammy"],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert (
            findings == []
        )  # noble/resolute are supported but past the until bound

    def test_cloud_scoped_machine_types_excludes_undeclared_types(
        self, catalog
    ):
        # Worked example #5: gcp.pro is never a candidate, even though it
        # appears nowhere in coverage either.
        scenario = _scenario(
            combos={("jammy", "aws.pro")},
            tags=[
                "@releases.lts.supported",
                "@releases.machine_types:aws.pro",
                "@releases.machine_types:azure.pro",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        pairs = {(f.release, f.machine_type) for f in findings}
        assert not any(
            machine_type == "gcp.pro" for _release, machine_type in pairs
        )
        assert pairs == {
            ("noble", "aws.pro"),
            ("noble", "azure.pro"),
            ("resolute", "aws.pro"),
            ("resolute", "azure.pro"),
            ("jammy", "azure.pro"),
        }

    def test_temporary_exception_excludes_while_active(self, catalog):
        # Worked example #6, still within the exception window at TODAY.
        scenario = _scenario(
            combos={("bionic", "lxd-container")},
            tags=[
                "@releases.lts.supported",
                "@releases.lts.esm",
                "@releases.skip.noble.until.2026-08-15",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        missing = {f.release for f in findings}
        assert missing == {"focal", "jammy", "resolute"}

    def test_temporary_exception_reappears_once_expired(self, catalog):
        scenario = _scenario(
            combos={("bionic", "lxd-container")},
            tags=[
                "@releases.lts.supported",
                "@releases.lts.esm",
                "@releases.skip.noble.until.2026-01-01",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert "noble" in {f.release for f in findings}

    def test_permanent_exception_never_reappears(self, catalog):
        # Worked example #7.
        scenario = _scenario(
            combos={("bionic", "lxd-container")},
            tags=[
                "@releases.lts.supported",
                "@releases.lts.esm",
                "@releases.skip.noble",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert {f.release for f in findings} == {"focal", "jammy", "resolute"}

    def test_no_existing_coverage_and_no_since_leaves_the_line_unbounded(
        self, catalog
    ):
        # If nothing is covered yet on this line, there's no data to
        # derive a `since` default from, so none is applied.
        scenario = _scenario(
            combos=set(),
            tags=[
                "@releases.lts.supported",
                "@releases.lts.esm",
                "@releases.machine_types:lxd-container",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert {f.release for f in findings} == {
            "bionic",
            "focal",
            "jammy",
            "noble",
            "resolute",
        }

    def test_explicit_since_bound_narrows_the_otherwise_unbounded_window(
        self, catalog
    ):
        # Worked example #13 (reason is documentation-only; not parsed).
        scenario = _scenario(
            combos=set(),
            tags=[
                "@releases.lts.supported",
                "@releases.lts.esm",
                "@releases.since.lts.jammy",
                "@releases.machine_types:lxd-container",
            ],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert {f.release for f in findings} == {"jammy", "noble", "resolute"}

    def test_deliberately_fixed_never_flags_anything(self, catalog):
        # Worked example #10.
        scenario = _scenario(combos=set(), tags=["@releases.fixed"])
        assert find_gaps(catalog, [scenario], today=TODAY) == []

    def test_unclassified_scenario_is_flagged_not_guessed(self, catalog):
        # Worked example #11.
        scenario = _scenario(combos={("jammy", "lxd-container")}, tags=[])
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert len(findings) == 1
        assert findings[0].status == GapStatus.UNCLASSIFIED


# ---------------------------------------------------------------------------
# Error paths: malformed tags, unresolvable releases, and non-golden shapes
# must be surfaced as findings, never silently dropped or crash the run.
# ---------------------------------------------------------------------------
class TestErrorPaths:
    def test_unknown_release_in_since_bound_is_a_tag_error(self, catalog):
        scenario = _scenario(
            combos={("jammy", "lxd-container")},
            tags=["@releases.lts.supported", "@releases.since.lts.warty"],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert len(findings) == 1
        assert findings[0].status == GapStatus.TAG_ERROR
        assert "warty" in findings[0].detail

    def test_malformed_tag_is_a_tag_error_not_a_crash(self, catalog):
        scenario = _scenario(combos=set(), tags=["@releases.lts.bogus"])
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert len(findings) == 1
        assert findings[0].status == GapStatus.TAG_ERROR

    def test_non_matrix_driven_scenario_with_combos_is_non_standard_shape(
        self, catalog
    ):
        scenario = _scenario(
            name="Call Livepatched CVEs endpoint",
            combos={("xenial", "lxd-vm")},
            is_outline=False,
            example_columns=[],
        )
        findings = find_gaps(catalog, [scenario], today=TODAY)
        assert len(findings) == 1
        assert findings[0].status == GapStatus.NON_STANDARD_SHAPE

    def test_non_matrix_driven_scenario_with_no_combos_is_silent(
        self, catalog
    ):
        # A plain Scenario unrelated to release coverage (no Examples at
        # all) has nothing to flag -- only resolving combos without the
        # golden shape is the problem.
        scenario = _scenario(
            combos=set(), is_outline=False, example_columns=[]
        )
        assert find_gaps(catalog, [scenario], today=TODAY) == []

    def test_aggregation_of_mixed_shapes_is_non_standard(self, catalog):
        golden = _scenario(name="Mixed", combos={("jammy", "lxd-container")})
        malformed = _scenario(
            name="Mixed",
            combos={("jammy", "lxd-container")},
            is_outline=False,
            example_columns=[],
        )
        findings = find_gaps(catalog, [golden, malformed], today=TODAY)
        assert len(findings) == 1
        assert findings[0].status == GapStatus.NON_STANDARD_SHAPE
