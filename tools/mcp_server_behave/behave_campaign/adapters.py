"""Concrete adapters implementing the ports in ``behave_campaign.ports``.

Two stores satisfy ``CampaignStore`` because the two front-ends address
campaigns differently: the MCP names a campaign by id inside a campaign
directory, while the CLI is pointed at one campaign file. Both share the
serialisation and locking helpers below, so there is one on-disk format.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Sequence

from behave_campaign import discovery
from behave_campaign.domain import (
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    Record,
    Unit,
    encode_record,
    parse_record,
)
from behave_campaign.ports import (
    CampaignExistsError,
    CampaignLockedError,
    CampaignNotFoundError,
    LaneStartError,
)

CAMPAIGN_SUFFIX = ".jsonl"
EVENTS_SUFFIX = ".events" + CAMPAIGN_SUFFIX
LOCK_SUFFIX = ".lock"


def system_now() -> str:
    """Return the current UTC time in the shape campaign records use.

    Both front-ends take their clock from here so a campaign file has one
    timestamp format no matter which of them created it.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode_lines(records: Sequence[Record]) -> str:
    return "".join(
        json.dumps(
            encode_record(record), sort_keys=True, separators=(",", ":")
        )
        + "\n"
        for record in records
    )


def _append_locked(stream: IO[str], payload: str) -> None:
    """Append under an exclusive file lock, then force it to disk."""
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    try:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _create_file(path: Path, records: Sequence[Record], label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # "x" makes the existence check and the create one step, so two
        # callers racing on the same campaign cannot both believe they won.
        with path.open("x") as stream:
            _append_locked(stream, _encode_lines(records))
    except FileExistsError:
        raise CampaignExistsError(
            "{} already holds a campaign; create a new one instead".format(
                label
            )
        ) from None


def _append_file(path: Path, records: Sequence[Record]) -> None:
    if not records:
        return
    with path.open("a") as stream:
        _append_locked(stream, _encode_lines(records))


def _replay_file(path: Path) -> list[Record]:
    records: list[Record] = []
    with path.open("r") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                records.append(parse_record(json.loads(line)))
            except ValueError as error:
                raise CampaignError(
                    "invalid campaign record in {} on line {}: {}".format(
                        path.name, number, error
                    )
                ) from error
    return records


class JsonlCampaignStore:
    """Stores each campaign as ``<campaign_id>.jsonl`` under one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, campaign_id: str) -> Path:
        return self._root / (campaign_id + CAMPAIGN_SUFFIX)

    def create(
        self,
        campaign_id: str,
        header: CampaignHeader,
        plans: Sequence[PlanRecord],
    ) -> None:
        _create_file(
            self.path_for(campaign_id),
            [header, *plans],
            "campaign {!r}".format(campaign_id),
        )

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        _append_file(self._existing_path(campaign_id), records)

    def replay(self, campaign_id: str) -> list[Record]:
        return _replay_file(self._existing_path(campaign_id))

    def exists(self, campaign_id: str) -> bool:
        return self.path_for(campaign_id).is_file()

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            path.name[: -len(CAMPAIGN_SUFFIX)]
            for path in self._root.glob("*" + CAMPAIGN_SUFFIX)
            if path.is_file() and not path.name.endswith(EVENTS_SUFFIX)
        )

    def _existing_path(self, campaign_id: str) -> Path:
        path = self.path_for(campaign_id)
        if not path.is_file():
            raise CampaignNotFoundError(
                "no campaign {!r} under {}".format(campaign_id, self._root)
            )
        return path


class SingleFileCampaignStore:
    """Stores one campaign at an exact path, whatever it is named.

    The CLI is pointed at a file rather than at a campaign directory, so the
    file name is taken as given instead of being derived from an id.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def root(self) -> Path:
        return self._path.parent

    @property
    def campaign_id(self) -> str:
        return self._path.stem

    def path_for(self, campaign_id: str) -> Path:
        return self._path

    def create(
        self,
        campaign_id: str,
        header: CampaignHeader,
        plans: Sequence[PlanRecord],
    ) -> None:
        _create_file(self._path, [header, *plans], "this file")

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        _append_file(self._path, records)

    def replay(self, campaign_id: str) -> list[Record]:
        if not self._path.is_file():
            return []
        return _replay_file(self._path)

    def exists(self, campaign_id: str) -> bool:
        return self._path.is_file()

    def list_ids(self) -> list[str]:
        return [self.campaign_id] if self._path.is_file() else []


class ParserFeatureReader:
    """Reads feature files through ``behave_mcp.parser``."""

    def discover_units(self, repo_root: Path, filters: Filters) -> list[Unit]:
        return discovery.discover_units(repo_root, filters)

    def available_dimensions(self, repo_root: Path) -> dict[str, Any]:
        return discovery.available_dimensions(repo_root)


class ServiceLaneRunner:
    """Opens lanes by starting behave jobs through ``BehaveService``.

    One lane is one job is one unit: a single scenario, for one release, on
    one machine type.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def start(self, unit: Unit, *, repo_root: Path, install_from: str) -> str:
        result = self._service.start_scenario(
            feature_file=unit.feature,
            machine_types=[unit.machine_type],
            scenario_name=unit.scenario,
            releases=[unit.release],
            repo_root=str(repo_root),
            install_from=install_from,
        )
        if getattr(result, "status", "") != "started":
            raise LaneStartError(
                getattr(result, "error", "") or "could not start a job"
            )
        return str(result.job_id)

    def poll(self, job_id: str) -> Any | None:
        status = self._service.job_status(job_id)
        if getattr(status, "status", "") != "completed":
            return None
        return status.model_dump()


class FileCampaignRunLock:
    """Advisory ``flock`` on ``<campaign_id>.lock`` in the campaign dir.

    The kernel drops the lock when the holding process dies, so a crashed
    server leaves nothing to clean up by hand.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._held: dict[str, IO[str]] = {}

    def path_for(self, campaign_id: str) -> Path:
        return self._root / (campaign_id + LOCK_SUFFIX)

    def acquire(self, campaign_id: str) -> None:
        if campaign_id in self._held:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        stream = self.path_for(campaign_id).open("w")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise CampaignLockedError(
                "campaign {!r} is being run by another process".format(
                    campaign_id
                )
            ) from None
        stream.write(str(os.getpid()))
        stream.flush()
        self._held[campaign_id] = stream

    def release(self, campaign_id: str) -> None:
        stream = self._held.pop(campaign_id, None)
        if stream is None:
            return
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def held_by_other(self, campaign_id: str) -> bool:
        if campaign_id in self._held:
            return False
        path = self.path_for(campaign_id)
        if not path.is_file():
            return False
        with path.open("r") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False
