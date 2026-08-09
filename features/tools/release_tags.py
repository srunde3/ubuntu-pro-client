#!/usr/bin/env python3
"""Parse ``@releases:*``/``@machine_types:*`` behave tags into a
``CoverageDeclaration``.

Implements the vocabulary in
``dev-docs/reference/release_coverage_tags.md``, which encodes the fields
of ``dev-docs/explanation/release_coverage_model.md`` (``tracks``,
``since``/``until``, ``machine_types``, ``exceptions``) as Gherkin tags.
Keep this module and that document in sync when either changes.

This module only parses tags -- it never opens a ``.feature`` file or talks
to the MCP. Tags come in as plain strings (e.g. from the MCP's
``describe_feature`` response); a ``CoverageDeclaration`` comes out. It
does read one other file, ``machine_types.yaml`` -- see
``MACHINE_TYPES_TO_RELEASES`` below -- since ``ALLOWED_MACHINE_TYPES`` is
derived from it rather than kept as a separately hand-maintained list.

A ``reason`` (the human-facing "why") can never be recovered from a tag --
Gherkin tags can't contain whitespace, so reasons live in comments per the
encoding doc. Every ``reason`` field here is always ``None``; it exists so
this module's types line up with the information model's, not because this
parser can populate it.

TODO: strip reason from this model entirely, if we can't support it here.
Simpler to add back later.
"""

import os
from dataclasses import dataclass, field
from datetime import date
from typing import (
    Dict,
    FrozenSet,
    List,
    NewType,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import yaml

from features.tools.release_catalog import Series

TAG_PREFIX = "releases:"
MACHINE_TYPE_PREFIX = "machine_types:"
#: Every tag prefix this vocabulary claims -- used by callers (e.g.
#: ``coverage_gaps.py``) that need to recognize a coverage-relevant tag
#: without re-deriving this module's grammar.
COVERAGE_TAG_PREFIXES = (TAG_PREFIX, MACHINE_TYPE_PREFIX)

LINES = {"lts", "interim"}
#: Flat bucket name -> (line, status) it expands to. ``interim`` has only
#: ever had one status in practice, so its bucket tag drops the suffix;
#: ``lts`` keeps all three since its lifecycle actually varies.
BUCKETS: Dict[str, Tuple[str, str]] = {
    "lts_supported": ("lts", "supported"),
    "lts_esm": ("lts", "esm"),
    "lts_legacy": ("lts", "legacy"),
    "interim": ("interim", "supported"),
}

MachineType = NewType("MachineType", str)
Tag = NewType("Tag", str)

_MACHINE_TYPES_PATH = os.path.join(
    os.path.dirname(__file__), "machine_types.yaml"
)


def _load_machine_types() -> Dict[MachineType, Set[Series]]:
    with open(_MACHINE_TYPES_PATH, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return {
        MachineType(machine_type): {Series(release) for release in releases}
        for machine_type, releases in raw.items()
    }


MACHINE_TYPES_TO_RELEASES: Dict[MachineType, Set[Series]] = (
    _load_machine_types()
)

ALLOWED_MACHINE_TYPES: FrozenSet[MachineType] = frozenset(
    MACHINE_TYPES_TO_RELEASES.keys()
)


class TagValidationError(ValueError):
    """A ``@releases:*``/``@machine_types:*`` tag (or combination of tags)
    is malformed."""


@dataclass(frozen=True)
class Bound:
    """A ``since``/``until`` boundary. ``reason`` is always ``None`` here --
    see the module docstring."""

    release: Series
    reason: Optional[str] = None


@dataclass(frozen=True)
class SkipException:
    """One ``@releases:skip:*`` exception. ``machine_type=None`` means the
    whole release; ``expires=None`` means permanent. ``reason`` is always
    ``None`` here -- see the module docstring."""

    release: Series
    machine_type: Optional[MachineType]
    expires: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class CoverageDeclaration:
    """The parsed ``@releases:*``/``@machine_types:*`` facts for one
    scenario.

    ``tracks`` has three states, per the information model:

    * ``None`` -- no bucket tag (``@releases:lts_supported`` etc.) or
      ``@releases:fixed`` tag was present. ``UNCLASSIFIED``.
    * ``{}`` -- ``@releases:fixed`` was present. Deliberately tracks
      nothing, forever.
    * non-empty -- the declared ``{line: {status, ...}}`` buckets.
    """

    tracks: Optional[Dict[str, Set[str]]] = None
    since: Dict[str, Bound] = field(default_factory=dict)
    until: Dict[str, Bound] = field(default_factory=dict)
    machine_types: Set[MachineType] = field(default_factory=set)
    exceptions: List[SkipException] = field(default_factory=list)

    @property
    def is_unclassified(self) -> bool:
        return self.tracks is None


def _parse_date(tag: str, value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise TagValidationError(f"{tag!r}: {value!r} is not an ISO 8601 date")
    return value


def _parse_skip(tag: str, parts: List[str]) -> SkipException:
    # parts is what follows "skip" in `releases:skip:...`, already split on
    # `:`, so it's one of:
    #   [<release_key>]
    #   [<release_key>, "until", <date>]
    # where <release_key> is `<release>` or `<release>+<machine_type>`.
    if len(parts) == 1:
        key_part, expires = parts[0], None
    elif len(parts) == 3 and parts[1] == "until":
        key_part, expires = parts[0], _parse_date(tag, parts[2])
    else:
        raise TagValidationError(
            f"{tag!r}: expected skip:<release>[+<machine_type>]"
            "[:until:<date>]"
        )

    machine_type: Optional[MachineType]
    if "+" in key_part:
        release, raw_machine_type = key_part.split("+", 1)
        if raw_machine_type not in ALLOWED_MACHINE_TYPES:
            raise TagValidationError(
                f"{tag!r}: unknown machine_type {raw_machine_type!r}"
            )
        machine_type = MachineType(raw_machine_type)
    else:
        release, machine_type = key_part, None

    if not release:
        raise TagValidationError(f"{tag!r}: missing release")

    return SkipException(
        release=Series(release), machine_type=machine_type, expires=expires
    )


def _apply_machine_type(
    tag: Tag, raw_machine_type: str, machine_types: Set[MachineType]
) -> None:
    if raw_machine_type not in ALLOWED_MACHINE_TYPES:
        raise TagValidationError(
            f"{tag!r}: unknown machine_type {raw_machine_type!r}"
        )
    machine_types.add(MachineType(raw_machine_type))


def _apply_bound(
    tag: Tag,
    keyword: str,
    line: str,
    release: str,
    since: Dict[str, Bound],
    until: Dict[str, Bound],
) -> None:
    if line not in LINES:
        raise TagValidationError(f"{tag!r}: unknown line {line!r}")
    if not release:
        raise TagValidationError(f"{tag!r}: missing release")
    bucket = since if keyword == "since" else until
    if line in bucket:
        raise TagValidationError(
            f"{tag!r}: duplicate @releases:{keyword}:{line}:* tag"
        )
    bucket[line] = Bound(release=Series(release))


def _apply_skip(
    tag: Tag,
    parts: List[str],
    exceptions: List[SkipException],
    seen_exception_keys: Set[Tuple[Series, Optional[MachineType]]],
) -> None:
    exception = _parse_skip(tag, parts)
    key = (exception.release, exception.machine_type)
    if key in seen_exception_keys:
        raise TagValidationError(
            f"{tag!r}: duplicate @releases:skip:* for {key}"
        )
    seen_exception_keys.add(key)
    exceptions.append(exception)


def parse_tags(tags: Sequence[Tag]) -> CoverageDeclaration:
    """Parse every ``@releases:*``/``@machine_types:*`` tag in ``tags`` into
    a ``CoverageDeclaration``. Tags outside those namespaces (e.g.
    ``@uses.config.*``) are ignored.

    Raises ``TagValidationError`` on any malformed tag, an unknown
    line/status/machine_type token, ``@releases:fixed`` co-occurring with a
    tracked bucket tag, more than one ``since``/``until`` for the same
    line, or more than one ``@releases:skip:*`` for the same (release,
    machine_type) key. Nothing is silently dropped.
    """
    tracks: Dict[str, Set[str]] = {}
    classified = False
    fixed = False
    since: Dict[str, Bound] = {}
    until: Dict[str, Bound] = {}
    machine_types: Set[MachineType] = set()
    exceptions: List[SkipException] = []
    seen_exception_keys: Set[Tuple[Series, Optional[MachineType]]] = set()

    for tag in tags:
        if tag.startswith(MACHINE_TYPE_PREFIX):
            _apply_machine_type(
                tag, tag[len(MACHINE_TYPE_PREFIX) :], machine_types
            )
            continue
        if not tag.startswith(TAG_PREFIX):
            continue
        remainder = tag[len(TAG_PREFIX) :]

        # Every shape in this namespace is `:`-delimited -- a bare bucket
        # name, or `<keyword>:...` for since/until/skip. One split up front
        # is enough to dispatch to the right handler below. The final
        # `else` is the exhaustiveness backstop: every shape this
        # vocabulary defines is claimed by a branch above it, so anything
        # left is genuinely unrecognized, not just unhandled.
        parts = remainder.split(":")
        if parts == ["fixed"]:
            fixed = True
            classified = True
        elif len(parts) == 1 and parts[0] in BUCKETS:
            line, status = BUCKETS[parts[0]]
            tracks.setdefault(line, set()).add(status)
            classified = True
        elif len(parts) == 3 and parts[0] in ("since", "until"):
            _apply_bound(tag, parts[0], parts[1], parts[2], since, until)
        elif len(parts) >= 2 and parts[0] == "skip":
            _apply_skip(tag, parts[1:], exceptions, seen_exception_keys)
        else:
            raise TagValidationError(f"{tag!r}: unrecognized @releases:* tag")

    if fixed and tracks:
        raise TagValidationError(
            "@releases:fixed cannot co-occur with a tracked bucket tag"
        )

    return CoverageDeclaration(
        tracks=tracks if classified else None,
        since=since,
        until=until,
        machine_types=machine_types,
        exceptions=exceptions,
    )
