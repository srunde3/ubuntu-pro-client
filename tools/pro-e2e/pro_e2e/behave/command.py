"""The behave run for one unit."""

import re
from pathlib import Path

from pro_e2e.jobs.domain.jobs import Unit


def exact_name_pattern(scenario: str) -> str:
    """A behave ``--name`` regex matching this scenario and nothing else.

    behave names each Scenario Outline row ``<name> -- @<row> <examples>``,
    so the row suffix is allowed; any other longer name is not.
    """
    return "^{}(?: -- @.*)?$".format(re.escape(scenario))


def build_command(unit: Unit, json_report_path: Path) -> list[str]:
    """``tox -e behave`` selecting exactly ``unit``, writing its JSON
    report to ``json_report_path``."""
    command = ["tox", "-e", "behave", "--", unit.feature]
    command.extend(["--name", exact_name_pattern(unit.scenario)])
    command.extend(["-D", f"releases={unit.release}"])
    command.extend(["-D", f"machine_types={unit.machine_type}"])
    command.extend(["-f", "json", "-o", str(json_report_path)])
    command.extend(["-f", "plain"])
    return command
