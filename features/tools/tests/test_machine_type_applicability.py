from datetime import date

import pytest

from features.tools.machine_type_applicability import (
    UnknownReleaseError,
    applicable,
)
from features.tools.release_catalog import ReleaseCatalog

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
    assert applicable(catalog, "aws.pro", "xenial")
    assert applicable(catalog, "aws.pro", "resolute")


def test_lxd_types_have_explicit_entries_too(catalog):
    assert applicable(catalog, "lxd-container", "xenial")
    assert applicable(catalog, "lxd-vm", "resolute")


def test_unknown_machine_type_defaults_to_unbounded(catalog):
    assert applicable(catalog, "some-future-machine-type", "xenial")


@pytest.mark.parametrize(
    "release,expected",
    [
        ("xenial", True),
        ("bionic", True),
        ("focal", True),
        ("jammy", False),
        ("noble", False),
        ("resolute", False),
    ],
)
def test_fips_list_ends_at_focal(catalog, release, expected):
    assert applicable(catalog, "aws.pro-fips", release) is expected


@pytest.mark.parametrize(
    "release,expected",
    [
        ("xenial", False),  # never offered on xenial
        ("bionic", True),
        ("focal", True),
        ("jammy", False),
    ],
)
def test_gcp_fips_list_starts_at_bionic(catalog, release, expected):
    assert applicable(catalog, "gcp.pro-fips", release) is expected


@pytest.mark.parametrize(
    "release,expected",
    [
        ("xenial", False),
        ("bionic", True),
        ("jammy", True),
        ("noble", False),
        ("resolute", False),
    ],
)
def test_wsl_list_is_bionic_through_jammy(catalog, release, expected):
    assert applicable(catalog, "wsl", release) is expected


def test_unknown_release_raises(catalog):
    with pytest.raises(UnknownReleaseError):
        applicable(catalog, "aws.pro-fips", "warty")
