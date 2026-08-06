import os
from datetime import date

import pytest

from features.tools import release_catalog
from features.tools.release_catalog import (
    Release,
    ReleaseCatalog,
    load_csv_rows,
)

# A small, chronologically-ordered sample mirroring ubuntu.csv rows.
# All status math below is relative to TODAY.
TODAY = date(2026, 8, 1)

ROWS = [
    {
        "series": "xenial",
        "version": "16.04 LTS",
        "created": "2015-10-22",
        "release": "2016-04-21",
        "eol": "2021-04-30",
        "eol-esm": "2026-04-23",  # already past TODAY -> not esm
    },
    {
        "series": "bionic",
        "version": "18.04 LTS",
        "created": "2017-10-19",
        "release": "2018-04-26",
        "eol": "2023-05-31",
        "eol-esm": "2028-04-26",
    },
    {
        "series": "focal",
        "version": "20.04 LTS",
        "created": "2019-10-17",
        "release": "2020-04-23",
        "eol": "2025-05-29",
        "eol-esm": "2030-04-23",
    },
    {
        "series": "jammy",
        "version": "22.04 LTS",
        "created": "2021-10-14",
        "release": "2022-04-21",
        "eol": "2027-06-01",
        "eol-esm": "2032-04-21",
    },
    {
        "series": "noble",
        "version": "24.04 LTS",
        "created": "2023-10-12",
        "release": "2024-04-25",
        "eol": "2029-05-31",
        "eol-esm": "2034-04-25",
    },
    {
        "series": "questing",
        "version": "25.10",
        "created": "2025-04-17",
        "release": "2025-10-09",
        "eol": "2026-07-09",  # before TODAY -> eol
    },
    {
        "series": "resolute",
        "version": "26.04 LTS",
        "created": "2025-10-09",
        "release": "2026-04-23",
        "eol": "2031-05-29",
        "eol-esm": "2036-04-23",
    },
    {
        "series": "stonking",
        "version": "26.10",
        "created": "2026-04-24",
        "release": "2026-10-15",  # after TODAY -> devel
        "eol": "2027-07-15",
    },
]
CODENAMES = [row["series"] for row in ROWS]


@pytest.fixture
def catalog():
    return ReleaseCatalog.from_rows(ROWS, today=TODAY)


class TestFromRows:
    def test_preserves_chronological_order(self, catalog):
        assert [r.series for r in catalog.ordered] == CODENAMES

    def test_order_index_matches_position(self, catalog):
        assert catalog.order_of("xenial") == 0
        assert catalog.order_of("stonking") == 7

    def test_status_derivation(self, catalog):
        # xenial: not supported, not esm (esm eol is past) -> eol
        assert catalog.get("xenial").status == "eol"
        assert catalog.get("bionic").status == "esm"
        assert catalog.get("jammy").status == "supported"
        assert catalog.get("resolute").status == "supported"
        assert catalog.get("questing").status == "eol"
        assert catalog.get("stonking").status == "devel"

    def test_lts_detection(self, catalog):
        assert catalog.get("resolute").is_lts is True
        assert catalog.get("stonking").is_lts is False

    def test_dates_from_rows(self, catalog):
        resolute = catalog.get("resolute")
        assert resolute.released == "2026-04-23"
        assert resolute.eol_esm == "2036-04-23"

    def test_missing_optional_fields_leave_dates_none(self):
        catalog = ReleaseCatalog.from_rows(
            [{"series": "mystery"}], today=TODAY
        )
        mystery = catalog.get("mystery")
        assert mystery.version == ""
        assert mystery.released is None
        assert mystery.is_lts is False
        assert mystery.status == "eol"


class TestLookups:
    def test_known(self, catalog):
        assert catalog.known("noble") is True
        assert catalog.known("warty") is False

    def test_order_of_unknown_is_none(self, catalog):
        assert catalog.order_of("warty") is None

    def test_between_is_exclusive(self, catalog):
        series = [r.series for r in catalog.between(0, 6)]
        assert series == ["bionic", "focal", "jammy", "noble", "questing"]

    def test_newer_than(self, catalog):
        assert [r.series for r in catalog.newer_than(6)] == ["stonking"]

    def test_relevant_excludes_eol(self, catalog):
        relevant = [r.series for r in catalog.relevant()]
        assert "xenial" not in relevant  # eol
        assert "questing" not in relevant  # eol
        assert relevant == [
            "bionic",
            "focal",
            "jammy",
            "noble",
            "resolute",
            "stonking",
        ]


class TestLoadCsvRows:
    def test_reads_rows_in_order(self, tmp_path):
        csv_file = tmp_path / "ubuntu.csv"
        csv_file.write_text(
            "version,codename,series,created,release,eol,"
            "eol-server,eol-esm,eol-legacy\n"
            "16.04 LTS,Xenial Xerus,xenial,2015-10-22,2016-04-21,"
            "2021-04-30,2021-04-30,2026-04-23,2031-04-30\n"
            "26.04 LTS,Resolute Raccoon,resolute,2025-10-09,"
            "2026-04-23,2031-05-29,2031-05-29,2036-04-23,2041-04-30\n"
        )
        rows = load_csv_rows(str(csv_file))
        assert [row["series"] for row in rows] == ["xenial", "resolute"]
        assert rows[1]["version"] == "26.04 LTS"
        assert rows[1]["eol-esm"] == "2036-04-23"
        assert rows[1]["eol-legacy"] == "2041-04-30"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_csv_rows(str(tmp_path / "nope.csv"))


class TestFromCsv:
    def test_reads_csv_and_computes_status(self, tmp_path):
        csv_file = tmp_path / "ubuntu.csv"
        csv_file.write_text(
            "version,codename,series,created,release,eol,"
            "eol-server,eol-esm,eol-legacy\n"
            "22.04 LTS,Jammy Jellyfish,jammy,2021-10-14,2022-04-21,"
            "2027-06-01,2027-06-01,2032-04-21,2037-04-30\n"
        )
        catalog = ReleaseCatalog.from_csv(str(csv_file), today=TODAY)
        assert catalog.get("jammy").status == "supported"


class TestStatusProperty:
    def test_priority_ordering(self):
        # devel must win over every other flag.
        release = Release(
            "x", 0, supported=True, supported_esm=True, devel=True
        )
        assert release.status == "devel"

    def test_esm_only(self):
        assert Release("x", 0, supported_esm=True).status == "esm"

    def test_esm_beats_legacy(self):
        release = Release("x", 0, supported_esm=True, supported_legacy=True)
        assert release.status == "esm"

    def test_legacy_only(self):
        assert Release("x", 0, supported_legacy=True).status == "legacy"

    def test_eol_when_no_flags(self):
        assert Release("x", 0).status == "eol"


class TestLegacySupportWindow:
    """Legacy add-on support has no distro-info equivalent; it's derived
    from the eol-esm -> eol-legacy dates in ubuntu.csv."""

    ROW = {
        "series": "trusty",
        "version": "14.04 LTS",
        "eol": "2019-04-25",
        "eol-esm": "2024-04-25",
        "eol-legacy": "2029-04-26",
    }

    def _catalog(self, today):
        return ReleaseCatalog.from_rows([self.ROW], today=today)

    def test_legacy_when_esm_ended_but_legacy_window_open(self):
        trusty = self._catalog(today=date(2026, 8, 1)).get("trusty")
        assert trusty.supported_legacy is True
        assert trusty.status == "legacy"

    def test_not_legacy_while_still_in_esm(self):
        trusty = self._catalog(today=date(2024, 1, 1)).get("trusty")
        assert trusty.supported_legacy is False
        assert trusty.status == "esm"

    def test_eol_after_legacy_window_ends(self):
        trusty = self._catalog(today=date(2030, 1, 1)).get("trusty")
        assert trusty.supported_legacy is False
        assert trusty.status == "eol"

    def test_no_eol_legacy_data_is_not_legacy(self):
        catalog = ReleaseCatalog.from_rows(ROWS, today=TODAY)
        assert catalog.get("xenial").supported_legacy is False

    def test_legacy_is_relevant(self):
        catalog = self._catalog(today=date(2026, 8, 1))
        assert "trusty" in [r.series for r in catalog.relevant()]


@pytest.mark.skipif(
    not os.path.exists(release_catalog.UBUNTU_CSV),
    reason="ubuntu.csv is not installed",
)
class TestIntegrationRealCsv:
    """Exercises the real ``ubuntu.csv`` database end to end.

    Assertions are intentionally date- and machine-independent so the
    test stays stable as releases come and go.
    """

    @pytest.fixture(scope="class")
    def catalog(self):
        return ReleaseCatalog.from_csv()

    def test_catalog_is_non_empty(self, catalog):
        assert catalog.ordered

    def test_well_known_releases_present_and_ordered(self, catalog):
        for series in ("xenial", "bionic", "focal", "jammy", "noble"):
            assert catalog.known(series)
        # Ordering is chronological.
        assert (
            catalog.order_of("xenial")
            < catalog.order_of("bionic")
            < catalog.order_of("focal")
        )

    def test_lts_detection_against_real_data(self, catalog):
        assert catalog.get("bionic").is_lts is True

    def test_devel_is_a_subset_of_supported(self, catalog):
        for release in catalog.ordered:
            if release.devel:
                assert release.supported

    def test_has_at_least_one_supported_release(self, catalog):
        assert any(r.status == "supported" for r in catalog.ordered)

    def test_metadata_is_populated(self, catalog):
        assert any(r.version for r in catalog.ordered)
