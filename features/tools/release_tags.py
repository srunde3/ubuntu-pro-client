#!/usr/bin/env python3
"""Parse ``@releases.*`` behave tags into a ``CoverageDeclaration``.

Implements the vocabulary in
``dev-docs/reference/release_coverage_tags.md``, which encodes the fields
of ``dev-docs/explanation/release_coverage_model.md`` (``tracks``,
``since``/``until``, ``machine_types``, ``exceptions``) as Gherkin tags.
Keep this module and that document in sync when either changes.

This module only parses tags -- it never opens a ``.feature`` file or talks
to the MCP. Tags come in as plain strings (e.g. from the MCP's
``describe_feature`` response); a ``CoverageDeclaration`` comes out.

A ``reason`` (the human-facing "why") can never be recovered from a tag --
Gherkin tags can't contain whitespace, so reasons live in comments per the
encoding doc. Every ``reason`` field here is always ``None``; it exists so
this module's types line up with the information model's, not because this
parser can populate it.

TODO: strip reason from this model entirely, if we can't support it here.
Simpler to add back later.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, NewType, Optional, Sequence, Set

from features.tools.release_catalog import Series

TAG_PREFIX = "releases."
LINES = {"lts", "interim"}
STATUSES = {"supported", "esm", "legacy"}

MachineType = NewType("MachineType", str)
Tag = NewType("Tag", str)

ALLOWED_MACHINE_TYPES: Set[MachineType] = {
    MachineType("lxd-container"),
    MachineType("lxd-vm"),
    MachineType("aws.generic"),
    MachineType("gcp.generic"),
    MachineType("azure.generic"),
    MachineType("aws.pro"),
    MachineType("gcp.pro"),
    MachineType("azure.pro"),
    MachineType("aws.pro-fips"),
    MachineType("gcp.pro-fips"),
    MachineType("azure.pro-fips"),
    MachineType("wsl"),
}


class TagValidationError(ValueError):
    """A ``@releases.*`` tag (or combination of tags) is malformed."""


@dataclass(frozen=True)
class Bound:
    """A ``since``/``until`` boundary. ``reason`` is always ``None`` here --
    see the module docstring."""

    release: Series
    reason: Optional[str] = None


@dataclass(frozen=True)
class SkipException:
    """One ``@releases.skip.*`` exception. ``machine_type=None`` means the
    whole release; ``expires=None`` means permanent. ``reason`` is always
    ``None`` here -- see the module docstring."""

    release: Series
    machine_type: Optional[MachineType]
    expires: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class CoverageDeclaration:
    """The parsed ``@releases.*`` facts for one scenario.

    ``tracks`` has three states, per the information model:

    * ``None`` -- no ``@releases.<line>.<status>`` or ``@releases.fixed``
      tag was present. ``UNCLASSIFIED``.
    * ``{}`` -- ``@releases.fixed`` was present. Deliberately tracks
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


def _parse_skip(tag: str, rest: str) -> SkipException:
    # rest is one of:
    #   <release>
    #   <release>.until.<date>
    #   <release>+<machine_type>
    #   <release>+<machine_type>.until.<date>
    if ".until." in rest:
        key_part, expires = rest.split(".until.", 1)
        expires = _parse_date(tag, expires)
    else:
        key_part, expires = rest, None

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


def parse_tags(tags: Sequence[Tag]) -> CoverageDeclaration:
    """Parse every ``@releases.*`` tag in ``tags`` into a
    ``CoverageDeclaration``. Tags outside the ``@releases.*`` namespace
    (e.g. ``@uses.config.*``) are ignored.

    Raises ``TagValidationError`` on any malformed tag, an unknown
    line/status/machine_type token, ``@releases.fixed`` co-occurring with a
    ``@releases.<line>.<status>`` tag, more than one ``since``/``until`` for
    the same line, or more than one ``@releases.skip.*`` for the same
    (release, machine_type) key. Nothing is silently dropped.
    """
    tracks: Optional[Dict[str, Set[str]]] = None
    fixed = False
    since: Dict[str, Bound] = {}
    until: Dict[str, Bound] = {}
    machine_types: Set[MachineType] = set()
    exceptions: List[SkipException] = []
    seen_exception_keys: Set[tuple] = set()

    for tag in tags:
        if not tag.startswith(TAG_PREFIX):
            continue
        remainder = tag[len(TAG_PREFIX) :]

        if remainder == "fixed":
            fixed = True
            if tracks is None:
                tracks = {}
            continue

        if remainder.startswith("machine_types:"):
            raw_machine_type = remainder[len("machine_types:") :]
            if raw_machine_type not in ALLOWED_MACHINE_TYPES:
                raise TagValidationError(
                    f"{tag!r}: unknown machine_type {raw_machine_type!r}"
                )
            machine_types.add(MachineType(raw_machine_type))
            continue

        if remainder.startswith("since.") or remainder.startswith("until."):
            keyword, _, rest = remainder.partition(".")
            parts = rest.split(".", 1)
            if len(parts) != 2:
                raise TagValidationError(
                    f"{tag!r}: expected {keyword}.<line>.<release>"
                )
            line, release = parts
            if line not in LINES:
                raise TagValidationError(f"{tag!r}: unknown line {line!r}")
            if not release:
                raise TagValidationError(f"{tag!r}: missing release")
            bucket = since if keyword == "since" else until
            if line in bucket:
                raise TagValidationError(
                    f"{tag!r}: duplicate @releases.{keyword}.{line}.* tag"
                )
            bucket[line] = Bound(release=Series(release))
            continue

        if remainder.startswith("skip."):
            exception = _parse_skip(tag, remainder[len("skip.") :])
            key = (exception.release, exception.machine_type)
            if key in seen_exception_keys:
                raise TagValidationError(
                    f"{tag!r}: duplicate @releases.skip.* for {key}"
                )
            seen_exception_keys.add(key)
            exceptions.append(exception)
            continue

        # Only remaining valid shape: @releases.<line>.<status>
        parts = remainder.split(".")
        if len(parts) != 2:
            raise TagValidationError(f"{tag!r}: unrecognized @releases.* tag")
        line, status = parts
        if line not in LINES:
            raise TagValidationError(f"{tag!r}: unknown line {line!r}")
        if status not in STATUSES:
            raise TagValidationError(f"{tag!r}: unknown status {status!r}")
        if tracks is None:
            tracks = {}
        tracks.setdefault(line, set()).add(status)

    if fixed and tracks:
        raise TagValidationError(
            "@releases.fixed cannot co-occur with @releases.<line>.<status>"
        )

    return CoverageDeclaration(
        tracks=tracks,
        since=since,
        until=until,
        machine_types=machine_types,
        exceptions=exceptions,
    )
