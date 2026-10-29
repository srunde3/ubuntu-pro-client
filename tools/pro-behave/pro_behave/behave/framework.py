"""behave as the test framework: the ``Framework`` port's adapter."""

from pathlib import Path
from typing import Mapping, Sequence

from pro_behave.behave import parser
from pro_behave.behave.command import build_command
from pro_behave.behave.environment import job_environment
from pro_behave.behave.logs import digest_log
from pro_behave.behave.report import observe
from pro_behave.jobs.domain import catalog
from pro_behave.jobs.domain.jobs import Invocation, Unit
from pro_behave.jobs.domain.logs import LogDigest
from pro_behave.jobs.domain.results import ObservedInstance
from pro_behave.jobs.messages import Instance


def to_combo(combo: parser.Combo) -> catalog.Combo:
    return catalog.Combo(
        release=combo.release, machine_type=combo.machine_type
    )


def to_examples_block(block: parser.ExamplesBlock) -> catalog.ExamplesBlock:
    return catalog.ExamplesBlock(
        name=block.name,
        tags=list(block.tags),
        combos=[to_combo(combo) for combo in block.combos],
    )


def to_scenario(scenario: parser.ScenarioSummary) -> catalog.ScenarioSummary:
    return catalog.ScenarioSummary(
        name=scenario.name,
        type=scenario.type,
        tags=list(scenario.tags),
        requires_config=list(scenario.requires_config),
        example_columns=list(scenario.example_columns),
        combos=[to_combo(combo) for combo in scenario.combos],
        examples=[to_examples_block(block) for block in scenario.examples],
    )


def to_feature(detail: parser.FeatureDetail) -> catalog.FeatureDetail:
    return catalog.FeatureDetail(
        path=detail.path,
        title=detail.title,
        tags=list(detail.tags),
        requires_config=list(detail.requires_config),
        scenarios=[to_scenario(scenario) for scenario in detail.scenarios],
    )


class BehaveFramework:
    """Feature files under ``features/``, run with ``tox -e behave``."""

    def catalog(self, repo_root: Path) -> list[catalog.FeatureDetail]:
        return [
            to_feature(detail)
            for detail in parser.discover_feature_details(repo_root)
        ]

    def invocation(
        self, repo_root: Path, unit: Unit, report: Path
    ) -> Invocation:
        feature = catalog.normalize_feature_arg(unit.feature)
        runs = parser.instances_of(
            repo_root / feature, unit.scenario, unit.release, unit.machine_type
        )
        return Invocation(
            command=build_command(unit, report),
            instances=[
                Instance(id="{}:{}".format(feature, run.line), name=run.name)
                for run in runs
            ],
        )

    def environment(
        self,
        inherited: Mapping[str, str],
        options: Mapping[str, str],
        install_from: str,
    ) -> dict[str, str]:
        return job_environment(inherited, options, install_from)

    def observe(
        self, report: Path, log: Path
    ) -> list[ObservedInstance] | None:
        return observe(report, log)

    def digest_log(self, lines: Sequence[str]) -> LogDigest:
        return digest_log(lines)
