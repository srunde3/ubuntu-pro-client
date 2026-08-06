#!/usr/bin/env python3
"""Source ``applicable(m, r)`` -- see
``dev-docs/reference/machine_type_applicability.md`` for the fact this
implements and why it needs its own source, separate from ``ubuntu.csv``.

The data lives in ``machine_type_applicability.yaml``, not here -- this
module is only the lookup logic, which shouldn't need to change when the
data does.

TODO: consolidate this file into other coverage analysis/tag logic files.
We do not need a separate file for processing.
"""

import os
from typing import Dict, Set

import yaml

from features.tools.release_catalog import ReleaseCatalog, Series
from features.tools.release_tags import MachineType

_DATA_PATH = os.path.join(
    os.path.dirname(__file__), "machine_type_applicability.yaml"
)


def _load_applicability() -> Dict[MachineType, Set[Series]]:
    with open(_DATA_PATH, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return {
        MachineType(machine_type): {Series(release) for release in releases}
        for machine_type, releases in raw.items()
    }


#: machine_type -> the set of release series it was, or is, offered on.
APPLICABILITY: Dict[MachineType, Set[Series]] = _load_applicability()


class UnknownReleaseError(ValueError):
    """The release being checked names a release the catalog doesn't
    know."""


def applicable(
    catalog: ReleaseCatalog, machine_type: MachineType, release: Series
) -> bool:
    """Whether ``machine_type`` was, or is, actually offered for ``release``,
    per ``APPLICABILITY``. Unbounded (``True`` everywhere) for any
    machine_type not listed there.
    """
    if catalog.order_of(release) is None:
        raise UnknownReleaseError(f"unknown release {release!r}")

    if machine_type not in APPLICABILITY:
        return True
    return release in APPLICABILITY[machine_type]
