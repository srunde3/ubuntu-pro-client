#!/usr/bin/env python3
"""Flag behave scenarios missing coverage for a currently-relevant release.

Where ``release_catalog`` answers "what releases exist, and what's their
status right now?" and ``release_tags`` answers "what does this scenario
declare it should cover?", this tool answers "given both of those, and what
a scenario actually covers today, is anything missing?"

See ``dev-docs/explanation/release_coverage_model.md`` for the derivation
this implements (``R(S)``, ``Excepted(S)``, ``Missing(S)``) and
``dev-docs/reference/release_coverage_tags.md`` for the ``@releases.*`` tag
vocabulary ``release_tags.py`` parses.

Design constraints:

* This module never opens a ``.feature`` file itself. It reads structured
  scenario/combo data from ``features.behave_features`` (the repo's
  authority on ``.feature`` file conventions -- see
  ``features/behave_features.py``), the same module the behave MCP server
  uses. Investigation and execution of the suite live in the MCP; this
  module only does the gap arithmetic.
* Applicability is read entirely from ``@releases.*`` tags -- there is no
  separate skip-record log or other side artifact. A scenario with no
  ``@releases.*`` tags is reported ``UNCLASSIFIED``, never silently treated
  as either fully covered or a gap.
* Every release whose *current* (line, status) matches one of a scenario's
  declared buckets is checked, not just the newest -- LTS support windows
  overlap, so multiple releases can be simultaneously ``supported``.

Usage::

    python3 features/tools/coverage_gaps.py --repo-root .
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

# `features/behave_features.py` is the repo's authority on how .feature
# files are structured -- this script is one of its consumers, alongside
# the behave MCP server. `release_catalog` lives in the top-level tools/
# (it's generic Ubuntu-release-lifecycle math, not feature-specific), so
# both the repo root (for `features.*`) and tools/ need to be reachable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tools"))

from release_catalog import ReleaseCatalog  # noqa: E402
from release_tags import (  # noqa: E402
    TAG_PREFIX,
    CoverageDeclaration,
    TagValidationError,
    parse_tags,
)

from features import behave_features  # noqa: E402


class UnknownReleaseError(ValueError):
    """A ``since``/``until`` tag names a release the catalog doesn't know."""


# ---------------------------------------------------------------------------
# Inputs: scenario coverage (from MCP `describe_feature`)
# ---------------------------------------------------------------------------
@dataclass
class ScenarioCoverage:
    """One scenario's current release x machine_type coverage and its raw
    tags.

    Built from ``features.behave_features``' structured data -- this module
    never opens a ``.feature`` file itself.
    """

    feature_file: str
    scenario_name: str
    is_outline: bool
    example_columns: List[str]
    combos: Set[Tuple[str, str]] = field(
        default_factory=set
    )  # (release, machine_type)
    tags: List[str] = field(default_factory=list)
    #: Set by aggregate_scenarios when nodes sharing a name carry different
    #: @releases.* tags. Non-None means "don't trust this record's tags."
    tag_conflict_detail: Optional[str] = None

    def releases_covered(self) -> Set[str]:
        return {release for release, _machine_type in self.combos}

    def machine_types_covered(self) -> Set[str]:
        return {machine_type for _release, machine_type in self.combos}

    def is_matrix_driven(self) -> bool:
        return self.is_outline and "release" in self.example_columns


def scenario_coverage_from_feature_detail(
    detail: behave_features.FeatureDetail,
) -> List[ScenarioCoverage]:
    """Turn one ``behave_features.FeatureDetail`` into one ScenarioCoverage
    record per raw scenario/outline node -- no aggregation yet.
    """
    coverage: List[ScenarioCoverage] = []
    for scenario in detail.scenarios:
        combos = {
            (combo.release, combo.machine_type) for combo in scenario.combos
        }
        coverage.append(
            ScenarioCoverage(
                feature_file=detail.path,
                scenario_name=scenario.name,
                is_outline=scenario.type == "scenario_outline",
                example_columns=list(scenario.example_columns),
                combos=combos,
                tags=list(scenario.tags),
            )
        )
    return coverage


def load_scenario_coverage(repo_root: Path) -> List[ScenarioCoverage]:
    """Read every feature file under ``repo_root`` and return one
    ScenarioCoverage record per raw scenario/outline node -- no aggregation
    yet.
    """
    return [
        s
        for detail in behave_features.discover_feature_details(repo_root)
        for s in scenario_coverage_from_feature_detail(detail)
    ]


def _release_tags(tags: Sequence[str]) -> frozenset:
    return frozenset(tag for tag in tags if tag.startswith(TAG_PREFIX))


def aggregate_scenarios(
    coverage: Sequence[ScenarioCoverage],
) -> List[ScenarioCoverage]:
    """Union coverage across scenario/outline nodes that share an exact
    (feature_file, scenario_name) -- the ``fix.feature`` "split by
    precondition" pattern. Identical names within one file MUST mean
    identical behavior (see dev-docs/explanation/release_coverage_model.md);
    this aggregation assumes that bound holds.

    ``@releases.*`` tags are a property of the *behavior*, not the node --
    every node sharing a name MUST carry identical ``@releases.*`` tags.
    A mismatch sets ``tag_conflict_detail`` on the result rather than
    silently picking one node's tags or merging them.

    If any contributing node isn't golden-shaped (not an outline, or no
    ``release`` column), the aggregate isn't either -- ``is_matrix_driven()``
    on the result reflects that, so a mixed group surfaces as
    NON_STANDARD_SHAPE rather than being silently blessed by its
    well-shaped siblings.
    """
    grouped: Dict[Tuple[str, str], List[ScenarioCoverage]] = {}
    for record in coverage:
        grouped.setdefault(
            (record.feature_file, record.scenario_name), []
        ).append(record)

    aggregated: List[ScenarioCoverage] = []
    for (feature_file, scenario_name), nodes in grouped.items():
        combined_combos: Set[Tuple[str, str]] = set()
        combined_columns: List[str] = []
        for node in nodes:
            combined_combos |= node.combos
            for column in node.example_columns:
                if column not in combined_columns:
                    combined_columns.append(column)

        distinct_tag_sets = {_release_tags(node.tags) for node in nodes}
        tag_conflict_detail = None
        if len(distinct_tag_sets) > 1:
            tag_sets = sorted(sorted(s) for s in distinct_tag_sets)
            tag_conflict_detail = (
                f"nodes sharing the name {scenario_name!r} carry different "
                f"@releases.* tags: {tag_sets}"
            )

        aggregated.append(
            ScenarioCoverage(
                feature_file=feature_file,
                scenario_name=scenario_name,
                is_outline=all(node.is_outline for node in nodes),
                example_columns=combined_columns,
                combos=combined_combos,
                tags=nodes[0].tags,
                tag_conflict_detail=tag_conflict_detail,
            )
        )
    return aggregated


# ---------------------------------------------------------------------------
# Derivation: R(S), Excepted(S), Missing(S)
# ---------------------------------------------------------------------------
def _resolve_order(catalog: ReleaseCatalog, release: str) -> int:
    order = catalog.order_of(release)
    if order is None:
        raise UnknownReleaseError(f"unknown release {release!r}")
    return order


def _line_of(release) -> str:
    return "lts" if release.is_lts else "interim"


def compute_r(
    catalog: ReleaseCatalog,
    scenario: ScenarioCoverage,
    declaration: CoverageDeclaration,
) -> Set[Tuple[str, str]]:
    """``R(S)``: every (release, machine_type) pair this scenario should
    currently cover, per its declared ``tracks``/``since``/``until``/
    ``machine_types``. Raises ``UnknownReleaseError`` if a ``since``/
    ``until`` tag names a release the catalog doesn't recognize.

    ``since``/``until`` are never inferred from ``scenario``'s current
    coverage -- unstated means unbounded on that side, same as ``until``
    already worked. A bound derived from "what's currently covered" would
    move every time coverage changes, which defeats the purpose of a
    coverage checker: deleting the *earliest* covered release would quietly
    narrow the requirement instead of surfacing the deletion as a gap.
    """
    machine_types = (
        declaration.machine_types or scenario.machine_types_covered()
    )
    if not machine_types:
        return set()

    releases: Set[str] = set()
    for line, statuses in (declaration.tracks or {}).items():
        since_bound = declaration.since.get(line)
        until_bound = declaration.until.get(line)
        since_order = (
            _resolve_order(catalog, since_bound.release)
            if since_bound
            else None
        )
        until_order = (
            _resolve_order(catalog, until_bound.release)
            if until_bound
            else None
        )
        for release in catalog.ordered:
            if _line_of(release) != line or release.status not in statuses:
                continue
            if since_order is not None and release.order < since_order:
                continue
            if until_order is not None and release.order > until_order:
                continue
            releases.add(release.series)

    return {
        (release, machine_type)
        for release in releases
        for machine_type in machine_types
    }


def compute_excepted(
    declaration: CoverageDeclaration,
    candidate: Set[Tuple[str, str]],
    today: date,
) -> Set[Tuple[str, str]]:
    """``Excepted(S)``: pairs in ``candidate`` covered by an unexpired
    ``@releases.skip.*`` exception -- either a specific (release,
    machine_type) or the whole release (``machine_type is None``).
    """
    excepted: Set[Tuple[str, str]] = set()
    for exception in declaration.exceptions:
        if (
            exception.expires is not None
            and date.fromisoformat(exception.expires) < today
        ):
            continue  # lapsed -- the pair is a live gap again
        if exception.machine_type is None:
            excepted |= {
                pair for pair in candidate if pair[0] == exception.release
            }
        else:
            excepted.add((exception.release, exception.machine_type))
    return excepted


def compute_missing(
    catalog: ReleaseCatalog,
    scenario: ScenarioCoverage,
    declaration: CoverageDeclaration,
    today: Optional[date] = None,
) -> Set[Tuple[str, str]]:
    """``Missing(S) = R(S) - Covered(S) - Excepted(S)``."""
    r = compute_r(catalog, scenario, declaration)
    excepted = compute_excepted(declaration, r, today or date.today())
    return r - scenario.combos - excepted


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
class GapStatus(str, Enum):
    GAP = "gap"  # in Missing(S) -- needs a new Examples row or a skip tag
    UNCLASSIFIED = "unclassified"  # no @releases.* tags at all -- undecided
    NON_STANDARD_SHAPE = "non_standard_shape"  # doesn't match the golden shape
    TAG_ERROR = "tag_error"  # malformed/conflicting/unresolvable tags


@dataclass
class Finding:
    feature_file: str
    scenario_name: str
    status: GapStatus
    release: str = ""
    machine_type: str = ""
    bucket: str = (
        ""  # e.g. "lts.supported" -- diagnostic only, not load-bearing
    )
    detail: str = ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def find_gaps(
    catalog: ReleaseCatalog,
    scenarios: Sequence[ScenarioCoverage],
    today: Optional[date] = None,
) -> List[Finding]:
    """Aggregate coverage, parse each scenario's tags, and derive
    ``Missing(S)`` for every golden-shaped, classified scenario.
    """
    findings: List[Finding] = []

    for scenario in aggregate_scenarios(scenarios):
        if scenario.tag_conflict_detail:
            findings.append(
                Finding(
                    scenario.feature_file,
                    scenario.scenario_name,
                    GapStatus.TAG_ERROR,
                    detail=scenario.tag_conflict_detail,
                )
            )
            continue

        if not scenario.is_matrix_driven():
            if scenario.combos:
                findings.append(
                    Finding(
                        scenario.feature_file,
                        scenario.scenario_name,
                        GapStatus.NON_STANDARD_SHAPE,
                        detail="resolves release coverage without matching "
                        "the golden Scenario Outline + Examples(release) "
                        "shape",
                    )
                )
            continue

        try:
            declaration = parse_tags(scenario.tags)
        except TagValidationError as exc:
            findings.append(
                Finding(
                    scenario.feature_file,
                    scenario.scenario_name,
                    GapStatus.TAG_ERROR,
                    detail=str(exc),
                )
            )
            continue

        if declaration.is_unclassified:
            findings.append(
                Finding(
                    scenario.feature_file,
                    scenario.scenario_name,
                    GapStatus.UNCLASSIFIED,
                )
            )
            continue

        try:
            missing = compute_missing(catalog, scenario, declaration, today)
        except UnknownReleaseError as exc:
            findings.append(
                Finding(
                    scenario.feature_file,
                    scenario.scenario_name,
                    GapStatus.TAG_ERROR,
                    detail=str(exc),
                )
            )
            continue

        for release, machine_type in sorted(missing):
            found = catalog.get(release)
            bucket = f"{_line_of(found)}.{found.status}" if found else ""
            findings.append(
                Finding(
                    scenario.feature_file,
                    scenario.scenario_name,
                    GapStatus.GAP,
                    release=release,
                    machine_type=machine_type,
                    bucket=bucket,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------
def render_table(findings: Sequence[Finding]) -> str:
    lines: List[str] = []
    for status in GapStatus:
        group = [f for f in findings if f.status == status]
        if not group:
            continue
        lines.append(f"-- {status.value} ({len(group)}) --")
        for f in sorted(
            group,
            key=lambda f: (
                f.feature_file,
                f.scenario_name,
                f.release,
                f.machine_type,
            ),
        ):
            lines.append(
                f"  {f.feature_file:<45} {f.scenario_name[:40]:<40} "
                f"{f.bucket:<15} {f.release:<10} {f.machine_type:<14} "
                f"{f.detail}"
            )
    return "\n".join(lines)


def render_json(findings: Sequence[Finding]) -> str:
    return json.dumps(
        [
            {
                "feature_file": f.feature_file,
                "scenario_name": f.scenario_name,
                "status": f.status.value,
                "release": f.release,
                "machine_type": f.machine_type,
                "bucket": f.bucket,
                "detail": f.detail,
            }
            for f in findings
        ],
        indent=2,
        sort_keys=True,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="path to the ubuntu-pro-client checkout to read features/ from "
        "(default: current directory)",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    catalog = ReleaseCatalog.from_csv()
    scenarios = load_scenario_coverage(Path(args.repo_root).resolve())

    findings = find_gaps(catalog, scenarios)
    print(
        render_json(findings)
        if args.format == "json"
        else render_table(findings)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
