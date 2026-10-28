import pytest

from pro_behave.campaign.domain.discovery import (
    CatalogScenario,
    discover_units,
)
from pro_behave.campaign.domain.records import CampaignError, Scope, Unit

FEATURE = "features/example.feature"
SHARED_FEATURE = "features/shared.feature"
JAMMY_LXD = ("jammy", "lxd-container")
NOBLE_VM = ("noble", "lxd-vm")

# One feature: a scenario on two combos, and one on containers only.
EVERYWHERE = CatalogScenario(FEATURE, "Runs everywhere", (JAMMY_LXD, NOBLE_VM))
CONTAINERS = CatalogScenario(FEATURE, "Runs on containers only", (JAMMY_LXD,))
EXAMPLE = ([FEATURE], [EVERYWHERE, CONTAINERS])


def shared(second_combo):
    """Two scenarios named "Shared"; the second's combo decides whether
    they collide."""
    return [
        CatalogScenario(SHARED_FEATURE, "Shared", (JAMMY_LXD,)),
        CatalogScenario(SHARED_FEATURE, "Shared", (second_combo,)),
    ]


def test_every_combo_becomes_a_unit():
    units = discover_units(*EXAMPLE, Scope())

    assert units == [
        Unit(FEATURE, "Runs everywhere", "jammy", "lxd-container"),
        Unit(FEATURE, "Runs everywhere", "noble", "lxd-vm"),
        Unit(FEATURE, "Runs on containers only", "jammy", "lxd-container"),
    ]


def test_release_filter_selects_a_campaign_slice():
    units = discover_units(*EXAMPLE, Scope(release=("noble",)))

    assert units == [Unit(FEATURE, "Runs everywhere", "noble", "lxd-vm")]


def test_machine_type_filter_selects_a_campaign_slice():
    units = discover_units(*EXAMPLE, Scope(machine_type=("lxd-container",)))

    assert {unit.release for unit in units} == {"jammy"}
    assert len(units) == 2


def test_filters_combine():
    scope = Scope(release=("jammy",), scenario=("Runs on containers only",))

    units = discover_units(*EXAMPLE, scope)

    assert units == [
        Unit(FEATURE, "Runs on containers only", "jammy", "lxd-container")
    ]


def test_a_scenario_listing_a_combo_twice_is_one_unit():
    twice = CatalogScenario(FEATURE, "Twice", (JAMMY_LXD, JAMMY_LXD))

    units = discover_units([FEATURE], [twice], Scope())

    assert units == [Unit(FEATURE, "Twice", "jammy", "lxd-container")]


def test_unknown_release_is_rejected():
    with pytest.raises(CampaignError, match="unknown release: bogus"):
        discover_units(*EXAMPLE, Scope(release=("bogus",)))


def test_unknown_machine_type_is_rejected():
    with pytest.raises(CampaignError, match="unknown machine_type"):
        discover_units(*EXAMPLE, Scope(machine_type=("lxd-toaster",)))


def test_unknown_feature_is_rejected():
    with pytest.raises(CampaignError, match="unknown feature"):
        discover_units(*EXAMPLE, Scope(feature=("features/nope.feature",)))


def test_mistyped_feature_suggests_close_matches():
    # Enough features that they are not all listed.
    features = ["features/example{}.feature".format(n) for n in range(20)]

    with pytest.raises(CampaignError) as error:
        discover_units(
            features, [], Scope(feature=("features/example7.featur",))
        )

    message = str(error.value)
    assert "did you mean: features/example7.feature" in message
    assert "features/example1.feature" not in message


def test_a_shared_name_on_a_shared_combo_is_refused():
    with pytest.raises(CampaignError) as error:
        discover_units([SHARED_FEATURE], shared(JAMMY_LXD), Scope())

    message = str(error.value)
    assert "1 unit(s) in scope are named by more than one scenario" in message
    assert "Shared jammy lxd-container" in message


def test_a_shared_name_on_other_combos_is_two_units():
    units = discover_units([SHARED_FEATURE], shared(NOBLE_VM), Scope())

    assert [(u.release, u.machine_type) for u in units] == [
        JAMMY_LXD,
        NOBLE_VM,
    ]


def test_a_scope_that_leaves_out_the_collision_is_allowed():
    features = [FEATURE, SHARED_FEATURE]
    scenarios = [EVERYWHERE, CONTAINERS, *shared(JAMMY_LXD)]

    units = discover_units(features, scenarios, Scope(feature=(FEATURE,)))

    assert {u.feature for u in units} == {FEATURE}
