"""Integration tests against the real ubuntu-pro-client features/ corpus.

Every other test file in this suite uses two small, hand-written fixture
feature files -- useful for exercising specific behavior precisely, but
unable to catch the class of bug that only shows up in real, messy
Gherkin (a step-text style the combo regex doesn't recognize, a
deliberately emptied Examples table, an authoring quirk nobody
anticipated). These tests build a real catalog from this checkout's
actual features/ directory and assert invariants that only a real corpus
can meaningfully check.

Excluded from the default `pytest` run (see pyproject.toml's `addopts`);
run explicitly with `pytest -m integration`.
"""

from pathlib import Path

import pytest

from behave_catalog.db import TestCatalog

pytestmark = pytest.mark.integration


def _find_repo_root(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / "features").is_dir():
            return candidate
    return None


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if REPO_ROOT is None:
    pytest.skip(
        "Could not locate the ubuntu-pro-client checkout root above this "
        "test file",
        allow_module_level=True,
    )

# A stable lower bound on scenario/feature counts -- loose enough not to
# need bumping as the suite grows, tight enough to catch a catastrophic
# regression (e.g. discovery or parsing silently returning nothing).
MIN_EXPECTED_FEATURES = 40
MIN_EXPECTED_SCENARIOS = 200

# LTS releases that will remain in the suite for years -- a stable set to
# assert dimensions() surfaces, unlike fast-rotating interim releases.
STABLE_LTS_RELEASES = {"bionic", "focal", "jammy", "noble"}

# Known, already-investigated pre-existing gaps in the real corpus as of
# this writing (a deliberately emptied Examples table in
# cli/attach.feature, and a step-text style -- literal machine_type with
# no backticks -- the combo regex doesn't recognize, in ubuntu_pro.feature)
# -- not something this test suite introduced. This is a ceiling, not a
# target: it must not silently grow, but fixing one of these three doesn't
# require touching this test.
MAX_KNOWN_ZERO_COMBO_SCENARIOS = 3


@pytest.fixture(scope="module")
def real_catalog(tmp_path_factory: pytest.TempPathFactory) -> TestCatalog:
    db_path = tmp_path_factory.mktemp("integration") / "catalog.sqlite3"
    with TestCatalog.build(REPO_ROOT, db_path) as catalog:
        yield catalog


def test_build_succeeds_and_finds_a_realistic_number_of_features(
    real_catalog: TestCatalog,
) -> None:
    entries = real_catalog.features()
    assert len(entries) >= MIN_EXPECTED_FEATURES


def test_scenario_count_is_realistic(real_catalog: TestCatalog) -> None:
    # total_count reflects every match regardless of limit -- no need to
    # actually fetch (and construct a ScenarioMatch for) all of them just
    # to check the count.
    assert real_catalog.query(limit=1).total_count >= MIN_EXPECTED_SCENARIOS


def test_dimensions_cover_known_stable_releases(
    real_catalog: TestCatalog,
) -> None:
    dims = real_catalog.dimensions()
    release_names = {value.name for value in dims.releases}
    assert STABLE_LTS_RELEASES <= release_names


def test_no_new_scenarios_silently_produce_zero_combos(
    real_catalog: TestCatalog,
) -> None:
    """A scenario with zero combos is invisible to every release/
    machine_type filter -- exactly the failure mode this whole catalog
    exists to avoid. This doesn't demand zero today (three pre-existing,
    already-investigated cases exist -- see MAX_KNOWN_ZERO_COMBO_SCENARIOS),
    but it must never get worse without someone noticing here."""
    all_scenarios = real_catalog.query(limit=100_000).matches
    zero_combo = [match for match in all_scenarios if not match.combos]
    assert len(zero_combo) <= MAX_KNOWN_ZERO_COMBO_SCENARIOS, (
        "New scenario(s) with zero combos (invisible to every "
        f"release/machine_type filter): "
        f"{[(m.feature_path, m.name) for m in zero_combo]}"
    )
