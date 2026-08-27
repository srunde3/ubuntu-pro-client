"""Integration tests for ``behave_mcp.parser`` against the real
``features/*.feature`` files.
"""

import pathlib

import pytest

from behave_mcp import parser

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_discover_feature_files_finds_real_features():
    paths = parser.discover_feature_files(_REPO_ROOT)

    assert len(paths) > 50
    assert all(path.startswith("features/") for path in paths)
    assert all(path.endswith(".feature") for path in paths)
    assert "features/cli/attach.feature" in paths


@pytest.mark.integration
def test_discover_feature_details_parses_every_real_feature_file():
    paths = parser.discover_feature_files(_REPO_ROOT)
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
