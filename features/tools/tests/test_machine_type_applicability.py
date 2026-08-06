# machine_type/release applicability lookup logic lives in coverage_gaps.py
# (merged from a standalone machine_type_applicability.py); kept as its own
# test file since it's a narrow, data-driven unit distinct from
# coverage_gaps.py's aggregation/gap-computation tests.
from datetime import date

import pytest

from features.tools.coverage_gaps import UnknownReleaseError, _applicable
from features.tools.release_catalog import ReleaseCatalog, Series
from features.tools.release_tags import MachineType

TODAY = date(2026, 8, 1)

# Same small ordered sample used elsewhere -- order is all this module needs.
ROWS = [
    {"series": "xenial", "version": "16.04 LTS", "eol": "2021-04-30"},
    {"series": "bionic", "version": "18.04 LTS", "eol": "2023-05-31"},
    {"series": "focal", "version": "20.04 LTS", "eol": "2025-05-29"},
    {"series": "jammy", "version": "22.04 LTS", "eol": "2027-06-01"},
    {"series": "noble", "version": "24.04 LTS", "eol": "2029-05-31"},
    {"series": "resolute", "version": "26.04 LTS", "eol": "2031-05-29"},
]


@pytest.fixture
def catalog():
    return ReleaseCatalog.from_rows(ROWS, today=TODAY)


def test_listed_machine_type_is_applicable_on_every_release_it_names(catalog):
    assert _applicable(catalog, MachineType("aws.pro"), Series("xenial"))
    assert _applicable(catalog, MachineType("aws.pro"), Series("resolute"))


def test_lxd_types_have_explicit_entries_too(catalog):
    assert _applicable(catalog, MachineType("lxd-container"), Series("xenial"))
    assert _applicable(catalog, MachineType("lxd-vm"), Series("resolute"))


def test_unknown_machine_type_defaults_to_unbounded(catalog):
    assert _applicable(
        catalog, MachineType("some-future-machine-type"), Series("xenial")
    )


@pytest.mark.parametrize(
    "release,expected",
    [
        (Series("xenial"), True),
        (Series("bionic"), True),
        (Series("focal"), True),
        (Series("jammy"), False),
        (Series("noble"), False),
        (Series("resolute"), False),
    ],
)
def test_fips_list_ends_at_focal(catalog, release, expected):
    assert (
        _applicable(catalog, MachineType("aws.pro-fips"), release) is expected
    )


@pytest.mark.parametrize(
    "release,expected",
    [
        (Series("xenial"), False),  # never offered on xenial
        (Series("bionic"), True),
        (Series("focal"), True),
        (Series("jammy"), False),
    ],
)
def test_gcp_fips_list_starts_at_bionic(catalog, release, expected):
    assert (
        _applicable(catalog, MachineType("gcp.pro-fips"), release) is expected
    )


@pytest.mark.parametrize(
    "release,expected",
    [
        (Series("xenial"), False),
        (Series("bionic"), True),
        (Series("jammy"), True),
        (Series("noble"), False),
        (Series("resolute"), False),
    ],
)
def test_wsl_list_is_bionic_through_jammy(catalog, release, expected):
    assert _applicable(catalog, MachineType("wsl"), release) is expected


def test_unknown_release_raises(catalog):
    with pytest.raises(UnknownReleaseError):
        _applicable(catalog, MachineType("aws.pro-fips"), Series("warty"))
