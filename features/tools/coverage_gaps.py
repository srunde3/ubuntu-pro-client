#!/usr/bin/env python3
"""Flag behave scenarios missing coverage for a currently-relevant release.

Where ``release_catalog`` answers "what releases exist, and what's their
status right now?" and ``release_tags`` answers "what does this scenario
declare it should cover?", this tool answers "given both of those, and what
a scenario actually covers today, is anything missing?"

See ``dev-docs/explanation/release_coverage_model.md`` for the derivation
this implements (``R(S)``, ``Excepted(S)``, ``Missing(S)``) and
``dev-docs/reference/release_coverage_tags.md`` for the ``@releases:*``/
``@machine_types:*`` tag vocabulary ``release_tags.py`` parses.

Design constraints:

* This module never opens a ``.feature`` file itself. It reads structured
  scenario/combo data from ``features.behave_features`` (the repo's
  authority on ``.feature`` file conventions -- see
  ``features/behave_features.py``), the same module the behave MCP server
  uses. Investigation and execution of the suite live in the MCP; this
  module only does the gap arithmetic.
* Applicability is read entirely from ``@releases:*``/``@machine_types:*``
  tags -- there is no separate skip-record log or other side artifact. A
  scenario with no ``@releases:*`` tags is reported ``UNCLASSIFIED``,
  never silently treated as either fully covered or a gap
* Every release whose *current* (line, status) matches one of a scenario's
  declared buckets is checked, not just the newest -- LTS support windows
  overlap, so multiple releases can be simultaneously ``supported``.

Usage::

    uv run --project features features/tools/coverage_gaps.py --repo-root .

``--project features`` is required: this script lives inside the
``pro-client-features`` package (``features/pyproject.toml``, which
declares its ``behave``/``pyyaml`` dependencies), but ``uv run`` doesn't
walk into subdirectories looking for a ``pyproject.toml`` on its own.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from features import behave_features
from features.tools.release_catalog import ReleaseCatalog, Series
from features.tools.release_tags import (
    COVERAGE_TAG_PREFIXES,
    MACHINE_TYPES_TO_RELEASES,
    CoverageDeclaration,
    MachineType,
    Tag,
    TagValidationError,
    parse_tags,
)


class UnknownReleaseError(ValueError):
    """A ``since``/``until`` tag names a release the catalog doesn't know."""


# ---------------------------------------------------------------------------
# Inputs: scenario coverage (from MCP `describe_feature`)
# ---------------------------------------------------------------------------
#: One `Examples:` block's raw tags and its own (release, machine_type)
#: rows -- (tags, combos).
_Block = Tuple[List[Tag], Set[Tuple[Series, MachineType]]]


@dataclass
class ScenarioCoverage:
    """One scenario/outline node's current release x machine_type coverage
    and its raw tags -- or, after ``aggregate_scenarios``, one aggregated
    (scenario_name, tag_set) policy group.

    Built from ``features.behave_features``' structured data -- this module
    never opens a ``.feature`` file itself.
    """

    feature_file: str
    scenario_name: str
    is_outline: bool
    example_columns: List[str]
    combos: Set[Tuple[Series, MachineType]] = field(
        default_factory=set
    )  # (release, machine_type)
    tags: List[Tag] = field(default_factory=list)
    #: Set by aggregate_scenarios when nodes sharing a name carry different
    #: @releases:*/@machine_types:* tags. Non-None means "don't trust
    #: this record's tags."
    tag_conflict_detail: Optional[str] = None
    #: Per-`Examples:` block tags+combos, when known. Real parsed Scenario
    #: Outlines always populate one entry per Examples table (even a single
    #: one) -- see behave_features.ExamplesBlock. Left empty for plain
    #: Scenarios and for synthetic/test-constructed records that only model
    #: one undifferentiated unit; ``effective_blocks`` falls back to
    #: ``tags``/``combos`` in that case, since there's no separate node
    #: label for those to be misplaced relative to.
    blocks: List[_Block] = field(default_factory=list)

    def releases_covered(self) -> Set[Series]:
        return {release for release, _machine_type in self.combos}

    def machine_types_covered(self) -> Set[MachineType]:
        return {machine_type for _release, machine_type in self.combos}

    def is_matrix_driven(self) -> bool:
        return self.is_outline and "release" in self.example_columns

    def effective_blocks(self) -> List[_Block]:
        return self.blocks or [(self.tags, self.combos)]


def scenario_coverage_from_feature_detail(
    detail: behave_features.FeatureDetail,
) -> List[ScenarioCoverage]:
    """Turn one ``behave_features.FeatureDetail`` into one ScenarioCoverage
    record per raw scenario/outline node -- no aggregation yet.
    """
    coverage: List[ScenarioCoverage] = []
    for scenario in detail.scenarios:
        combos = {
            (Series(combo.release), MachineType(combo.machine_type))
            for combo in scenario.combos
        }
        blocks: List[_Block] = [
            (
                [Tag(tag) for tag in block.tags],
                {
                    (Series(combo.release), MachineType(combo.machine_type))
                    for combo in block.combos
                },
            )
            for block in scenario.examples
        ]
        coverage.append(
            ScenarioCoverage(
                feature_file=detail.path,
                scenario_name=scenario.name,
                is_outline=scenario.type == "scenario_outline",
                example_columns=list(scenario.example_columns),
                combos=combos,
                tags=[Tag(tag) for tag in scenario.tags],
                blocks=blocks,
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


def _filter_release_tags(tags: Sequence[Tag]) -> FrozenSet[Tag]:
    return frozenset(
        tag for tag in tags if tag.startswith(COVERAGE_TAG_PREFIXES)
    )


def aggregate_scenarios(
    coverage: Sequence[ScenarioCoverage],
) -> List[ScenarioCoverage]:
    """Group scenario/outline nodes that share an exact (feature_file,
    scenario_name) -- the ``fix.feature`` "split by precondition" pattern --
    and, within a golden-shaped group, further split by each ``Examples:``
    block's own ``@releases:*``/``@machine_types:*`` tag content -- the
    "two Examples blocks with different testing policies" pattern (see
    ``dev-docs/reference/release_coverage_tags.md``). One
    (scenario_name, tag_set) pair produces one output ``ScenarioCoverage``,
    with combos unioned from every block -- in any node sharing this
    scenario_name -- carrying that exact tag_set.

    Two invariants are enforced, both producing a ``tag_conflict_detail``
    result rather than silently resolving:

    * **Tag placement.** A ``@releases:*``/``@machine_types:*`` tag
      directly on ``Scenario Outline:`` (rather than ``Examples:``) is
      always wrong, never a fallback -- checkable whenever a node's
      per-block tags are actually known (``node.blocks`` non-empty; real
      parsed outlines always populate this, even for a single Examples
      block).
    * **Cross-node consistency, for the classic single-block split.** When
      one scenario name spans multiple Scenario Outline nodes and every one
      of them has exactly one Examples block (the ``fix.feature`` shape),
      those blocks' tags must be identical -- a mismatch there is
      unambiguously a typo, not a deliberate choice. Once any node uses
      multiple blocks (sub-grouping is in play), this check is skipped: a
      sibling node legitimately covering only a subset of the declared
      policy groups (e.g. an ``@upgrade`` variant exercising one
      machine_type) is indistinguishable, from tags alone, from a forgotten
      group, so it's accepted -- each tag_set is evaluated with whatever
      combos actually carry it, across every contributing node.

    If any contributing node isn't golden-shaped (not an outline, or no
    ``release`` column), the whole group falls back to the old whole-node
    union -- ``is_matrix_driven()`` on the result reflects that, so a mixed
    group surfaces as NON_STANDARD_SHAPE rather than being silently blessed
    by its well-shaped siblings.
    """
    grouped: Dict[Tuple[str, str], List[ScenarioCoverage]] = {}
    for record in coverage:
        grouped.setdefault(
            (record.feature_file, record.scenario_name), []
        ).append(record)

    aggregated: List[ScenarioCoverage] = []
    for (feature_file, scenario_name), nodes in grouped.items():
        is_outline = all(node.is_outline for node in nodes)
        combined_columns: List[str] = []
        for node in nodes:
            for column in node.example_columns:
                if column not in combined_columns:
                    combined_columns.append(column)

        misplaced = [
            node
            for node in nodes
            if node.blocks and _filter_release_tags(node.tags)
        ]
        if misplaced:
            aggregated.append(
                ScenarioCoverage(
                    feature_file=feature_file,
                    scenario_name=scenario_name,
                    is_outline=is_outline,
                    example_columns=combined_columns,
                    tag_conflict_detail=(
                        "@releases:*/@machine_types:* tag(s) found on "
                        "Scenario Outline instead of Examples:: "
                        f"{sorted(_filter_release_tags(misplaced[0].tags))}"
                    ),
                )
            )
            continue

        if not (is_outline and "release" in combined_columns):
            combined_combos: Set[Tuple[Series, MachineType]] = set()
            for node in nodes:
                combined_combos |= node.combos
            distinct_tag_sets = {
                _filter_release_tags(node.tags) for node in nodes
            }
            tag_conflict_detail = None
            if len(distinct_tag_sets) > 1:
                tag_sets = sorted(sorted(s) for s in distinct_tag_sets)
                tag_conflict_detail = (
                    f"nodes sharing the name {scenario_name!r} carry "
                    f"different @releases:*/@machine_types:* tags: {tag_sets}"
                )
            aggregated.append(
                ScenarioCoverage(
                    feature_file=feature_file,
                    scenario_name=scenario_name,
                    is_outline=is_outline,
                    example_columns=combined_columns,
                    combos=combined_combos,
                    tags=nodes[0].tags,
                    tag_conflict_detail=tag_conflict_detail,
                )
            )
            continue

        node_block_tagsets = [
            [
                frozenset(_filter_release_tags(tags))
                for tags, _combos in node.effective_blocks()
            ]
            for node in nodes
        ]
        # The identical-tags invariant only applies when every node sharing
        # this name has exactly one Examples block -- the classic
        # precondition-split shape (fix.feature), where all nodes are
        # supposed to declare the same single policy and a mismatch is
        # unambiguously a typo. Once any node uses multiple blocks
        # (deliberate sub-grouping), a sibling node legitimately covering
        # only a subset of those policy groups (e.g. an `@upgrade` variant
        # that only exercises one machine_type) is indistinguishable, from
        # the tags alone, from someone forgetting to replicate a group --
        # so it's accepted rather than flagged, and each tag_set is simply
        # evaluated with whatever combos actually carry it.
        if len(nodes) > 1 and all(
            len(tag_sets) == 1 for tag_sets in node_block_tagsets
        ):
            distinct = {tag_sets[0] for tag_sets in node_block_tagsets}
            if len(distinct) > 1:
                tag_sets = sorted(sorted(s) for s in distinct)
                aggregated.append(
                    ScenarioCoverage(
                        feature_file=feature_file,
                        scenario_name=scenario_name,
                        is_outline=is_outline,
                        example_columns=combined_columns,
                        tag_conflict_detail=(
                            f"nodes sharing the name {scenario_name!r} "
                            "carry different @releases:*/@machine_types:*"
                            f" tags: {tag_sets}"
                        ),
                    )
                )
                continue

        combos_by_tagset: Dict[
            FrozenSet[Tag], Set[Tuple[Series, MachineType]]
        ] = {}
        for node in nodes:
            for tags, combos in node.effective_blocks():
                key = frozenset(_filter_release_tags(tags))
                combos_by_tagset.setdefault(key, set()).update(combos)

        for tag_set, combos in combos_by_tagset.items():
            aggregated.append(
                ScenarioCoverage(
                    feature_file=feature_file,
                    scenario_name=scenario_name,
                    is_outline=is_outline,
                    example_columns=combined_columns,
                    combos=combos,
                    tags=sorted(tag_set),
                )
            )
    return aggregated


# ---------------------------------------------------------------------------
# Derivation: R(S), Excepted(S), Missing(S)
# ---------------------------------------------------------------------------
def _resolve_order(catalog: ReleaseCatalog, release: Series) -> int:
    order = catalog.order_of(release)
    if order is None:
        raise UnknownReleaseError(f"unknown release {release!r}")
    return order


def _bound_orders(
    catalog: ReleaseCatalog, declaration: CoverageDeclaration, line: str
) -> Tuple[Optional[int], Optional[int]]:
    """The resolved (since_order, until_order) for ``line``, shared by both
    the ordinary bucket loop and the ``latest`` loop below -- a bound
    applies to a line regardless of which mechanism is requiring it."""
    since_bound = declaration.since.get(line)
    until_bound = declaration.until.get(line)
    since_order = (
        _resolve_order(catalog, since_bound.release) if since_bound else None
    )
    until_order = (
        _resolve_order(catalog, until_bound.release) if until_bound else None
    )
    return since_order, until_order


def compute_required_coverage(
    catalog: ReleaseCatalog,
    scenario: ScenarioCoverage,
    declaration: CoverageDeclaration,
) -> Set[Tuple[Series, MachineType]]:
    """``R(S)``: every (release, machine_type) pair this scenario should
    currently cover, per its declared ``tracks``/``latest``/``since``/
    ``until``/``machine_types``. Raises ``UnknownReleaseError`` if a
    ``since``/``until`` tag names a release the catalog doesn't recognize.

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

    releases: Set[Series] = set()
    for line, statuses in (declaration.tracks or {}).items():
        since_order, until_order = _bound_orders(catalog, declaration, line)
        for release in catalog.ordered:
            if release.line != line or release.status not in statuses:
                continue
            if since_order is not None and release.order < since_order:
                continue
            if until_order is not None and release.order > until_order:
                continue
            releases.add(release.series)

    for line in declaration.latest:
        latest_series = catalog.latest(line)
        if latest_series is None:
            continue
        since_order, until_order = _bound_orders(catalog, declaration, line)
        order = _resolve_order(catalog, latest_series)
        if since_order is not None and order < since_order:
            continue
        if until_order is not None and order > until_order:
            continue
        releases.add(latest_series)

    return {
        (release, machine_type)
        for release in releases
        for machine_type in machine_types
        if _applicable(catalog, machine_type, release)
    }


def _applicable(
    catalog: ReleaseCatalog, machine_type: MachineType, release: Series
) -> bool:
    """Whether ``machine_type`` was, or is, actually offered for
    ``release``, per ``MACHINE_TYPES_TO_RELEASES``. Unbounded (``True``
    everywhere) for any machine_type not listed there.
    """
    if catalog.order_of(release) is None:
        raise UnknownReleaseError(f"unknown release {release!r}")
    if machine_type not in MACHINE_TYPES_TO_RELEASES:
        return True
    return release in MACHINE_TYPES_TO_RELEASES[machine_type]


def compute_excepted(
    declaration: CoverageDeclaration,
    candidate: Set[Tuple[Series, MachineType]],
    today: date,
) -> Set[Tuple[Series, MachineType]]:
    """``Excepted(S)``: pairs in ``candidate`` covered by an unexpired
    ``@releases:skip:*`` exception -- either a specific (release,
    machine_type) or the whole release (``machine_type is None``).
    """
    excepted: Set[Tuple[Series, MachineType]] = set()
    for exception in declaration.exceptions:
        if (
            exception.expires is not None
            and date.fromisoformat(exception.expires) < today
        ):
            continue  # lapsed -- the pair is a live gap again
        machine_type = exception.machine_type
        if machine_type is None:
            excepted |= {
                pair for pair in candidate if pair[0] == exception.release
            }
        else:
            excepted.add((exception.release, machine_type))
    return excepted


def compute_missing(
    catalog: ReleaseCatalog,
    scenario: ScenarioCoverage,
    declaration: CoverageDeclaration,
    today: Optional[date] = None,
) -> Set[Tuple[Series, MachineType]]:
    """``Missing(S) = R(S) - Covered(S) - Excepted(S)``: every (release,
    machine_type) pair this scenario should currently cover but doesn't,
    and isn't deliberately excepted.
    """
    required = compute_required_coverage(catalog, scenario, declaration)
    excepted = compute_excepted(declaration, required, today or date.today())
    return required - scenario.combos - excepted


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
class GapStatus(str, Enum):
    GAP = "gap"  # in Missing(S) -- needs a new Examples row or a skip tag
    UNCLASSIFIED = "unclassified"  # no @releases:* tags at all -- undecided
    NON_STANDARD_SHAPE = "non_standard_shape"  # doesn't match the golden shape
    TAG_ERROR = "tag_error"  # malformed/conflicting/unresolvable tags


@dataclass
class Finding:
    feature_file: str
    scenario_name: str
    status: GapStatus
    release: Series = Series("")
    machine_type: MachineType = MachineType("")
    bucket: str = ""
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
                        "the standard Scenario Outline + Examples(release) "
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
            bucket = f"{found.line}_{found.status}" if found else ""
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
