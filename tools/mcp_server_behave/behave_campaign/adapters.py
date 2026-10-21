"""Concrete adapters implementing the ports in ``behave_campaign.ports``.

Two stores satisfy ``CampaignStore`` because the two front-ends address
campaigns differently: the MCP names a campaign by id inside a campaign
directory, while the CLI is pointed at one campaign file. Both share the
serialisation and locking helpers below, so there is one on-disk format.
"""

from __future__ import annotations

import fcntl
import gzip
import io
import json
import os
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Sequence

from behave_campaign.domain import discovery
from behave_campaign.domain.bundle import JOB_FILES, BundleEntry
from behave_campaign.domain.records import (
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    Record,
    Unit,
    encode_record,
    parse_record,
    parse_unit,
)
from behave_campaign.ports import (
    CampaignExistsError,
    CampaignLockedError,
    CampaignNotFoundError,
    LaneStartError,
)
from behave_mcp.domain import layout
from behave_mcp.domain.pools import Pool


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


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole or not at all, by temp and rename."""
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".{}.".format(path.name)
    )
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _create_campaign(
    paths: layout.CampaignPaths,
    header: CampaignHeader,
    plans: Sequence[PlanRecord],
    label: str,
) -> None:
    paths.campaign_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Making the directory is the existence check and the claim in one
        # step, so two callers racing on one id cannot both believe they won.
        paths.campaign_dir.mkdir()
    except FileExistsError:
        raise CampaignExistsError(
            "{} already holds a campaign; create a new one instead".format(
                label
            )
        ) from None
    document = {
        "campaign": encode_record(header),
        "units": [plan.unit.as_dict() for plan in plans],
    }
    _write_atomic(
        paths.record, json.dumps(document, sort_keys=True, indent=2) + "\n"
    )


def _append_log(
    paths: layout.CampaignPaths, records: Sequence[Record]
) -> None:
    """Append records under an exclusive lock, then force them to disk.

    A torn last line -- an append a crash cut short -- is cut off first, so
    the log stays whole lines however a writer died.
    """
    if not records:
        return
    with paths.log.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            size = stream.seek(0, os.SEEK_END)
            if size:
                stream.seek(size - 1)
                if stream.read(1) != b"\n":
                    stream.seek(0)
                    stream.truncate(stream.read().rfind(b"\n") + 1)
            stream.write(_encode_lines(records).encode())
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _replay_campaign(paths: layout.CampaignPaths) -> list[Record]:
    try:
        document = json.loads(paths.record.read_text())
        header = parse_record(document["campaign"])
        units = [parse_unit(raw) for raw in document["units"]]
    except (ValueError, KeyError, TypeError) as error:
        raise CampaignError(
            "invalid campaign record in {}: {}".format(paths.record, error)
        ) from error
    if not isinstance(header, CampaignHeader):
        raise CampaignError(
            "{} does not start with a campaign header".format(paths.record)
        )
    records: list[Record] = [
        header,
        *(PlanRecord(unit=unit, at=header.at) for unit in units),
    ]
    if not paths.log.is_file():
        return records
    text = paths.log.read_text()
    lines = text.split("\n")
    # Whatever follows the last newline is a torn append, never a record.
    for number, line in enumerate(lines[:-1], 1):
        if not line.strip():
            continue
        try:
            records.append(parse_record(json.loads(line)))
        except ValueError as error:
            raise CampaignError(
                "invalid campaign record in {} on line {}: {}".format(
                    paths.log, number, error
                )
            ) from error
    return records


class _Changes:
    """Wakes readers waiting for a campaign to grow.

    An append in this process wakes them at once. One made by another
    process is noticed within ``POLL_SECONDS``.
    """

    POLL_SECONDS = 1.0

    def __init__(self) -> None:
        self._changed = threading.Condition()

    def appended(self) -> None:
        with self._changed:
            self._changed.notify_all()

    def wait(
        self, count: Callable[[], int], seen: int, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        with self._changed:
            while count() <= seen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._changed.wait(min(remaining, self.POLL_SECONDS))


class JsonlCampaignStore:
    """Stores each campaign in its own directory under one root, laid out
    by ``layout.campaign_paths``."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._changes = _Changes()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, campaign_id: str) -> Path:
        return self._paths(campaign_id).campaign_dir

    def create(
        self,
        campaign_id: str,
        header: CampaignHeader,
        plans: Sequence[PlanRecord],
    ) -> None:
        _create_campaign(
            self._paths(campaign_id),
            header,
            plans,
            "campaign {!r}".format(campaign_id),
        )

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        _append_log(self._existing(campaign_id), records)
        if records:
            self._changes.appended()

    def replay(self, campaign_id: str) -> list[Record]:
        return _replay_campaign(self._existing(campaign_id))

    def wait(self, campaign_id: str, seen: int, timeout: float) -> None:
        self._changes.wait(
            lambda: len(self.replay(campaign_id)), seen, timeout
        )

    def exists(self, campaign_id: str) -> bool:
        return self._paths(campaign_id).record.is_file()

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            path.parent.name
            for path in self._root.glob(
                "*/{}".format(layout.CAMPAIGN_RECORD_NAME)
            )
        )

    def _paths(self, campaign_id: str) -> layout.CampaignPaths:
        return layout.campaign_paths(self._root, campaign_id)

    def _existing(self, campaign_id: str) -> layout.CampaignPaths:
        paths = self._paths(campaign_id)
        if not paths.record.is_file():
            raise CampaignNotFoundError(
                "no campaign {!r} under {}".format(campaign_id, self._root)
            )
        return paths


class CampaignDirStore:
    """Stores one campaign in a directory given outright.

    The CLI may be pointed at a campaign directory anywhere; its id is the
    directory's name.
    """

    def __init__(self, campaign_dir: Path) -> None:
        self._paths = layout.campaign_paths(
            campaign_dir.parent, campaign_dir.name
        )
        self._changes = _Changes()

    @property
    def root(self) -> Path:
        return self._paths.campaign_dir.parent

    @property
    def campaign_id(self) -> str:
        return self._paths.campaign_dir.name

    def path_for(self, campaign_id: str) -> Path:
        return self._paths.campaign_dir

    def create(
        self,
        campaign_id: str,
        header: CampaignHeader,
        plans: Sequence[PlanRecord],
    ) -> None:
        _create_campaign(self._paths, header, plans, "this directory")

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        _append_log(self._existing(), records)
        if records:
            self._changes.appended()

    def replay(self, campaign_id: str) -> list[Record]:
        return _replay_campaign(self._existing())

    def wait(self, campaign_id: str, seen: int, timeout: float) -> None:
        self._changes.wait(
            lambda: len(self.replay(campaign_id)), seen, timeout
        )

    def exists(self, campaign_id: str) -> bool:
        return self._paths.record.is_file()

    def list_ids(self) -> list[str]:
        return [self.campaign_id] if self._paths.record.is_file() else []

    def _existing(self) -> layout.CampaignPaths:
        if not self._paths.record.is_file():
            raise CampaignNotFoundError(
                "no campaign in {}".format(self._paths.campaign_dir)
            )
        return self._paths


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
            scenario_name=unit.scenario,
            release=unit.release,
            machine_type=unit.machine_type,
            repo_root=str(repo_root),
            install_from=install_from,
        )
        if getattr(result, "status", "") != "started":
            full_pools = getattr(result, "full_pools", None) or []
            raise LaneStartError(
                getattr(result, "error", "") or "could not start a job",
                full_pool=Pool(full_pools[0].pool) if full_pools else None,
            )
        return str(result.job_id)

    def poll(self, job_id: str) -> Any | None:
        status = self._service.job_status(job_id)
        if getattr(status, "status", "") != "completed":
            return None
        return status.model_dump()

    def kill(self, job_id: str) -> bool:
        return bool(self._service.kill_job(job_id).killed)


class FileCampaignRunLock:
    """Advisory ``flock`` on the campaign directory's ``campaign.lock``.

    The kernel drops the lock when the holding process dies, so a crashed
    server leaves nothing to clean up by hand.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._held: dict[str, IO[str]] = {}

    def path_for(self, campaign_id: str) -> Path:
        return layout.campaign_paths(self._root, campaign_id).lock

    def acquire(self, campaign_id: str) -> None:
        if campaign_id in self._held:
            return
        self.path_for(campaign_id).parent.mkdir(parents=True, exist_ok=True)
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


class JobDirFiles:
    """Reads job files from the behave MCP's shared ``jobs/`` directory."""

    def __init__(self, jobs_dir: Path) -> None:
        self._jobs_dir = jobs_dir

    def _path(self, job_id: str, name: str) -> Path:
        return self._jobs_dir / job_id / name

    def files(self, job_id: str) -> Mapping[str, Path]:
        found = {}
        for name in JOB_FILES:
            path = self._path(job_id, name)
            if path.is_file():
                found[name] = path
        return found


class TarBundleWriter:
    """Writes a gzipped tarball whose bytes depend only on its entries."""

    def write(
        self, output: Path, top: str, entries: Sequence[BundleEntry]
    ) -> list[str]:
        if output.exists():
            raise CampaignError(
                "{} already exists; choose another --output".format(output)
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=output.parent, prefix=".export-", suffix=".tmp"
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as raw:
                skipped = self._write_archive(raw, top, entries)
            # mkstemp makes the file private; use the umask's mode instead.
            umask = os.umask(0)
            os.umask(umask)
            temp.chmod(0o666 & ~umask)
            # Unlike a rename, a link never replaces an existing file.
            try:
                os.link(temp, output)
            except FileExistsError:
                raise CampaignError(
                    "{} already exists; choose another --output".format(output)
                ) from None
        finally:
            temp.unlink(missing_ok=True)
        return skipped

    @staticmethod
    def _write_archive(
        raw: IO[bytes], top: str, entries: Sequence[BundleEntry]
    ) -> list[str]:
        skipped = []
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for entry in sorted(entries, key=lambda e: e.name):
                    if entry.data is not None:
                        data = entry.data
                    elif entry.source is not None and entry.source.is_file():
                        data = entry.source.read_bytes()
                    else:
                        skipped.append(entry.name)
                        continue
                    info = tarfile.TarInfo("{}/{}".format(top, entry.name))
                    info.size = len(data)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
        return skipped
