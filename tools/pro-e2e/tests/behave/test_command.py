"""The behave command for one unit, and the name pattern that selects it."""

import subprocess
import sys

import pytest

from pro_e2e.behave.command import build_command, exact_name_pattern
from pro_e2e.jobs.domain.jobs import Unit


def test_build_command_escapes_scenario_name_for_behave_regex(tmp_path):
    unit = Unit(
        "features/proxy_config.feature",
        "apt_http(s)_proxy still works",
        "bionic",
        "lxd-container",
    )

    command = build_command(unit, tmp_path / "report.json")

    assert command[command.index("--name") + 1] == (
        r"^apt_http\(s\)_proxy\ still\ works(?: -- @.*)?$"
    )


def test_build_command_runs_one_release_on_one_machine_type(tmp_path):
    report = tmp_path / "report.json"
    unit = Unit(
        "features/cli/attach.feature", "Attach command", "jammy", "lxd-vm"
    )

    command = build_command(unit, report)

    assert command == [
        "tox",
        "-e",
        "behave",
        "--",
        "features/cli/attach.feature",
        "--name",
        exact_name_pattern("Attach command"),
        "-D",
        "releases=jammy",
        "-D",
        "machine_types=lxd-vm",
        "-f",
        "json",
        "-o",
        str(report),
        "-f",
        "plain",
    ]


@pytest.mark.integration
def test_exact_name_pattern_selects_one_scenario_and_its_rows(tmp_path):
    features = tmp_path / "features"
    (features / "steps").mkdir(parents=True)
    (features / "steps" / "steps.py").write_text(
        "from behave import given\n"
        "@given('step {x}')\n"
        "def step(context, x):\n"
        "    pass\n"
    )
    (features / "x.feature").write_text(
        "Feature: x\n"
        "  Scenario Outline: Attach command\n"
        "    Given step <release>\n"
        "    Examples: ubuntu release\n"
        "      | release |\n"
        "      | jammy   |\n"
        "      | noble   |\n"
        "\n"
        "  Scenario Outline: Attach command with token\n"
        "    Given step <release>\n"
        "    Examples:\n"
        "      | release |\n"
        "      | jammy   |\n"
        "\n"
        "  Scenario: Attach command (plain)\n"
        "    Given step x\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "behave",
            "--dry-run",
            "--no-summary",
            "-f",
            "plain",
            "--name",
            exact_name_pattern("Attach command"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    selected = [
        line.strip()
        for line in result.stdout.splitlines()
        if "Scenario" in line
    ]
    assert selected == [
        "Scenario Outline: Attach command -- @1.1 ubuntu release",
        "Scenario Outline: Attach command -- @1.2 ubuntu release",
    ]
