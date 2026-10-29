"""Integration tests for ``pro_e2e.behave.parser`` against the real
``features/*.feature`` files.
"""

import pathlib

import pytest

from pro_e2e.behave import parser

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


@pytest.mark.integration
def test_discover_feature_details_parses_every_real_feature_file():
    paths = list((_REPO_ROOT / "features").rglob("*.feature"))
    details = parser.discover_feature_details(_REPO_ROOT)

    # No real file is silently dropped due to a parse failure.
    assert len(details) == len(paths)
    assert all(detail.title for detail in details)
    assert sum(len(detail.scenarios) for detail in details) > 100


@pytest.mark.integration
def test_discover_feature_details_matches_known_attach_feature_shape():
    details = parser.discover_feature_details(_REPO_ROOT)
    by_path = {detail.path: detail for detail in details}

    attach = by_path["features/cli/attach.feature"]
    assert attach.title == "CLI attach command"
    assert attach.requires_config == ["contract_token"]

    scenarios_by_name = {s.name: s for s in attach.scenarios}
    expired = scenarios_by_name["Attach command failure on expired token"]
    assert expired.requires_config == [
        "contract_token",
        "contract_token_staging_expired",
    ]
    assert len(expired.combos) == 8
    assert all(
        combo.machine_type == "lxd-container" for combo in expired.combos
    )
    assert {combo.release for combo in expired.combos} == {
        "xenial",
        "bionic",
        "focal",
        "jammy",
        "noble",
        "resolute",
        "questing",
        "stonking",
    }
