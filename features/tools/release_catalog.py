#!/usr/bin/env python3
"""Authoritative Ubuntu release catalog, backed by ``ubuntu.csv``.

This tool answers a single question for the rest of the feature-test
maintenance workflow: *what are the Ubuntu releases, in order, and what is
each release's support status right now?*

Everything is derived from a single file, the ``distro-info-data``
package's ``ubuntu.csv``. Support status is computed by comparing those
dates to today:

* devel     -> created <= today < release
* supported -> today <= eol (covers devel too, since eol is already set)
* esm       -> eol < today <= eol-esm
* legacy    -> eol-esm < today <= eol-legacy (the Legacy add-on window)
* eol       -> none of the above

Usage::

    python3 features/tools/release_catalog.py            # human-readable table
    python3 features/tools/release_catalog.py --format json
"""

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, List, NewType, Optional, Sequence

UBUNTU_CSV = "/usr/share/distro-info/ubuntu.csv"

#: Statuses that count as "currently relevant" for gap analysis.
RELEVANT_STATUSES = ("devel", "supported", "esm", "legacy")

#: An Ubuntu release series name (e.g. ``"noble"``). A distinct type from
#: ``MachineType`` (see ``release_tags.py``) so the ubiquitous
#: ``(series, machine_type)`` pair can't be constructed with the two
#: swapped.
Series = NewType("Series", str)


@dataclass
class Release:
    series: Series
    order: int
    version: str = ""
    is_lts: bool = False
    created: Optional[str] = None
    released: Optional[str] = None
    eol: Optional[str] = None
    eol_esm: Optional[str] = None
    eol_legacy: Optional[str] = None
    supported: bool = False  # standard support (includes devel)
    supported_esm: bool = False
    supported_legacy: bool = False
    devel: bool = False

    @property
    def status(self) -> str:
        """Coarse support status: devel > supported > esm > legacy > eol."""
        if self.devel:
            return "devel"
        if self.supported:
            return "supported"
        if self.supported_esm:
            return "esm"
        if self.supported_legacy:
            return "legacy"
        return "eol"

    @property
    def is_relevant(self) -> bool:
        return self.status in RELEVANT_STATUSES


class ReleaseCatalog:
    """An ordered, queryable collection of :class:`Release` records."""

    def __init__(self, releases: Sequence[Release]) -> None:
        self.ordered: List[Release] = sorted(releases, key=lambda r: r.order)
        self._by_series: Dict[Series, Release] = {
            r.series: r for r in releases
        }

    # -- lookups ----------------------------------------------------------
    def get(self, series: Series) -> Optional[Release]:
        return self._by_series.get(series)

    def known(self, series: Series) -> bool:
        return series in self._by_series

    def order_of(self, series: Series) -> Optional[int]:
        release = self._by_series.get(series)
        return release.order if release else None

    def between(self, low: int, high: int) -> List[Release]:
        """Releases with ``low < order < high`` (exclusive on both ends)."""
        return [r for r in self.ordered if low < r.order < high]

    def newer_than(self, order: int) -> List[Release]:
        return [r for r in self.ordered if r.order > order]

    def relevant(self) -> List[Release]:
        return [r for r in self.ordered if r.is_relevant]

    # -- constructors -----------------------------------------------------
    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Dict[str, str]],
        today: Optional[date] = None,
    ) -> "ReleaseCatalog":
        """Build a catalog from ``ubuntu.csv`` rows.

        ``rows`` must be in chronological (oldest-first) order, as they
        appear in ``ubuntu.csv``; that order is the catalog's canonical
        ordering. Support status is computed from each row's dates
        relative to ``today`` (defaults to the real current date).
        """
        today = today or date.today()

        releases: List[Release] = []
        for order, row in enumerate(rows):
            version = row.get("version") or ""
            created = row.get("created") or None
            released = row.get("release") or None
            eol = row.get("eol") or None
            eol_esm = row.get("eol-esm") or None
            eol_legacy = row.get("eol-legacy") or None

            created_d = _parse_date(created)
            released_d = _parse_date(released)
            eol_d = _parse_date(eol)
            eol_esm_d = _parse_date(eol_esm)
            eol_legacy_d = _parse_date(eol_legacy)

            devel = (
                released_d is not None
                and today < released_d
                and (created_d is None or today >= created_d)
            )
            supported = devel or (eol_d is not None and today <= eol_d)
            supported_esm = (
                not supported and eol_esm_d is not None and today <= eol_esm_d
            )
            supported_legacy = (
                not supported
                and not supported_esm
                and eol_legacy_d is not None
                and today <= eol_legacy_d
            )

            releases.append(
                Release(
                    series=Series(row.get("series", "")),
                    order=order,
                    version=version,
                    is_lts="LTS" in version,
                    created=created,
                    released=released,
                    eol=eol,
                    eol_esm=eol_esm,
                    eol_legacy=eol_legacy,
                    supported=supported,
                    supported_esm=supported_esm,
                    supported_legacy=supported_legacy,
                    devel=devel,
                )
            )
        return cls(releases)

    @classmethod
    def from_csv(
        cls, csv_path: str = UBUNTU_CSV, today: Optional[date] = None
    ) -> "ReleaseCatalog":
        """Build a catalog by reading the ``ubuntu.csv`` database."""
        return cls.from_rows(load_csv_rows(csv_path), today=today)


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse an ISO ``YYYY-MM-DD`` date, tolerating blank/missing values."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# I/O helpers (kept thin and separate from the pure catalog logic)
# ---------------------------------------------------------------------------
def load_csv_rows(csv_path: str) -> List[Dict[str, str]]:
    """Read the ``distro-info`` CSV database, in on-disk (chronological)
    order.

    Raises if the file is missing: this is the catalog's only data
    source, so there is nothing useful to fall back to.
    """
    with open(csv_path, encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("series")]


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------
def render_table(catalog: ReleaseCatalog) -> str:
    header = "{:<10} {:<10} {:<10} {:<4} {:<12} {:<12}".format(
        "series", "version", "status", "lts", "release", "eol"
    )
    lines = [header, "-" * len(header)]
    for release in catalog.ordered:
        lines.append(
            "{:<10} {:<10} {:<10} {:<4} {:<12} {:<12}".format(
                release.series,
                release.version or "-",
                release.status,
                "yes" if release.is_lts else "-",
                release.released or "-",
                release.eol or "-",
            )
        )
    return "\n".join(lines)


def render_json(catalog: ReleaseCatalog) -> str:
    return json.dumps(
        [asdict(r) for r in catalog.ordered], indent=2, sort_keys=True
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="output format (default: table)",
    )
    parser.add_argument(
        "--csv-path",
        default=UBUNTU_CSV,
        help="path to the distro-info ubuntu.csv database",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    catalog = ReleaseCatalog.from_csv(args.csv_path)
    if args.format == "json":
        print(render_json(catalog))
    else:
        print(render_table(catalog))
    return 0


if __name__ == "__main__":
    sys.exit(main())
