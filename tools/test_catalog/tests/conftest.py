from pathlib import Path

import pytest

APT_FEATURE = """\
@uses.config.contract_token
Feature: apt behavior

  @security @slow
  Scenario Outline: Install a package on <release> <machine_type>
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I run `apt install curl` with sudo
    Then apt-get update runs successfully

    Examples: releases
    | release | machine_type  |
    | jammy   | lxd-container |
    | jammy   | lxd-vm        |
    | focal   | lxd-container |
"""

FIPS_FEATURE = """\
Feature: fips enablement

  Scenario: Enable FIPS on a fixed machine
    Given a `focal` `lxd-vm` machine with ubuntu-advantage-tools installed
    When I run `pro enable fips` with sudo
    Then fips is enabled
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    features_dir = tmp_path / "features"
    features_dir.mkdir()
    (features_dir / "apt.feature").write_text(APT_FEATURE)
    (features_dir / "fips.feature").write_text(FIPS_FEATURE)
    return tmp_path
