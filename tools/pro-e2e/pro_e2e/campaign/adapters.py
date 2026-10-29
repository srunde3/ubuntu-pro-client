"""Concrete adapters implementing the ports in ``pro_e2e.campaign.ports``.

Two stores satisfy ``CampaignStore`` because a campaign is addressed two
ways: a campaign id names a directory under the campaigns directory, and
the CLI can also be pointed at one campaign's directory. Both share the
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
from typing import IO, Any, Callable, ContextManager, Mapping, Sequence

from pro_e2e.campaign.domain import discovery
from pro_e2e.campaign.domain.bundle import JOB_FILES, BundleEntry
from pro_e2e.campaign.domain.classify import DIED, JOB_INTERRUPTED, JobStatus
from pro_e2e.campaign.domain.records import (
    CampaignError,
    CampaignRecord,
    Failure,
    InstanceResult,
    InstanceStatus,
    PlanRecord,
    Record,
    Scope,
    Unit,
    encode_record,
    parse_record,
    parse_unit,
)
from pro_e2e.campaign.ports import (
    CampaignExistsError,
    CampaignNotFoundError,
    QueueRefused,
)
from pro_e2e.jobs.domain import layout
from pro_e2e.jobs.domain.catalog import FeatureDetail


def system_now() -> str:
    """Return the current UTC time in the shape campaign records use.

    Both front-ends take their clock from here so a campaign file has one
    timestamp format no matter which of them created it.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    header: CampaignRecord,
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
        paths.campaign, json.dumps(document, sort_keys=True, indent=2) + "\n"
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
    with paths.records.open("a+b") as stream:
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
        document = json.loads(paths.campaign.read_text())
        header = parse_record(document["campaign"])
        units = [parse_unit(raw) for raw in document["units"]]
    except (ValueError, KeyError, TypeError) as error:
        raise CampaignError(
            "invalid campaign record in {}: {}".format(paths.campaign, error)
        ) from error
    if not isinstance(header, CampaignRecord):
        raise CampaignError(
            "{} does not start with a campaign header".format(paths.campaign)
        )
    records: list[Record] = [
        header,
        *(PlanRecord(unit=unit, at=header.at) for unit in units),
    ]
    if not paths.records.is_file():
        return records
    text = paths.records.read_text()
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
                    paths.records, number, error
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
        header: CampaignRecord,
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

    def read_records(self, campaign_id: str) -> list[Record]:
        return _replay_campaign(self._existing(campaign_id))

    def wait(self, campaign_id: str, seen: int, timeout: float) -> None:
        self._changes.wait(
            lambda: len(self.read_records(campaign_id)), seen, timeout
        )

    def exists(self, campaign_id: str) -> bool:
        return self._paths(campaign_id).campaign.is_file()

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            path.parent.name
            for path in self._root.glob(
                "*/{}".format(layout.CAMPAIGN_FILE_NAME)
            )
        )

    def _paths(self, campaign_id: str) -> layout.CampaignPaths:
        return layout.campaign_paths(self._root, campaign_id)

    def _existing(self, campaign_id: str) -> layout.CampaignPaths:
        paths = self._paths(campaign_id)
        if not paths.campaign.is_file():
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
        header: CampaignRecord,
        plans: Sequence[PlanRecord],
    ) -> None:
        _create_campaign(self._paths, header, plans, "this directory")

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        _append_log(self._existing(), records)
        if records:
            self._changes.appended()

    def read_records(self, campaign_id: str) -> list[Record]:
        return _replay_campaign(self._existing())

    def wait(self, campaign_id: str, seen: int, timeout: float) -> None:
        self._changes.wait(
            lambda: len(self.read_records(campaign_id)), seen, timeout
        )

    def exists(self, campaign_id: str) -> bool:
        return self._paths.campaign.is_file()

    def list_ids(self) -> list[str]:
        return [self.campaign_id] if self._paths.campaign.is_file() else []

    def _existing(self) -> layout.CampaignPaths:
        if not self._paths.campaign.is_file():
            raise CampaignNotFoundError(
                "no campaign in {}".format(self._paths.campaign_dir)
            )
        return self._paths


class CatalogFeatureReader:
    """Reads a checkout's units from the test framework's catalog.

    ``catalog`` is the job service's own, so a campaign plans against the
    catalog its jobs are validated against.
    """

    def __init__(self, catalog: Callable[[Path], list[FeatureDetail]]) -> None:
        self._catalog = catalog

    def discover_units(self, repo_root: Path, scope: Scope) -> list[Unit]:
        features = self._catalog(repo_root)
        if not features:
            raise CampaignError("no features found in {}".format(repo_root))
        return discovery.discover_units(
            [feature.path for feature in features],
            [
                discovery.CatalogScenario(
                    feature=feature.path,
                    scenario=scenario.name,
                    combos=tuple(
                        (combo.release, combo.machine_type)
                        for combo in scenario.combos
                    ),
                )
                for feature in features
                for scenario in feature.scenarios
            ],
            scope,
        )


def _value(field: Any) -> str:
    """An enum member's value, or the string itself."""
    return str(getattr(field, "value", field))


def job_status(response: Any) -> JobStatus:
    """A job service response, as the campaign's own ``JobStatus``.

    The campaign does not import the job service's types, so it reads the
    response's fields by name; a field a state lacks takes its default.
    """
    outcome = getattr(response, "outcome", None)
    interruption = getattr(response, "interruption", None)
    return JobStatus(
        state=_value(response.state),
        job_id=response.job_id,
        started_at=getattr(response, "started_at", "") or "",
        outcome=None if outcome is None else _value(outcome),
        reason=getattr(response, "reason", "") or "",
        interruption="" if interruption is None else _value(interruption),
        instances=tuple(
            InstanceResult(
                id=instance.id,
                name=instance.name,
                status=InstanceStatus(_value(instance.status)),
                reason=instance.reason,
            )
            for instance in getattr(response, "instances", [])
        ),
        failures=tuple(
            Failure(
                instance=failure.instance,
                step=failure.step,
                status=failure.status,
                error_message=failure.error_message,
            )
            for failure in getattr(response, "failures", [])
        ),
    )


class ServiceJobRunner:
    """Queues, reads and kills a campaign's jobs through ``JobService``.

    One job is one unit: a single scenario, for one release, on one machine
    type. The entry point that builds it passes the job service's exception
    types, since this package does not import that service: ``refusal`` for
    a unit it will not queue, ``unknown_job`` for a job id it never saw.
    """

    def __init__(
        self,
        service: Any,
        *,
        refusal: type[Exception],
        unknown_job: type[Exception],
    ) -> None:
        self._service = service
        self._refusal = refusal
        self._unknown_job = unknown_job

    def lock(self) -> ContextManager[None]:
        return self._service.reconcile_lock()

    def new_job_id(self) -> str:
        return str(self._service.new_job_id())

    def queue(
        self,
        job_id: str,
        unit: Unit,
        *,
        repo_root: Path,
        install_from: str,
        campaign_id: str,
        record: Callable[[], None],
    ) -> None:
        try:
            self._service.queue_job(
                unit.feature,
                unit.scenario,
                unit.release,
                unit.machine_type,
                repo_root=str(repo_root),
                install_from=install_from,
                job_id=job_id,
                campaign_id=campaign_id,
                record=record,
            )
        except self._refusal as error:
            # The unit stays queueable, and the campaign says why.
            raise QueueRefused(str(error), unit.pool) from error

    def poll(self, job_id: str) -> JobStatus:
        try:
            response = self._service.job_status(job_id)
        except self._unknown_job:
            # Recorded but never queued: the reconcile that would have
            # queued it died between the two.
            return JobStatus(
                state=JOB_INTERRUPTED, job_id=job_id, interruption=DIED
            )
        return job_status(response)

    def kill(self, job_id: str) -> bool:
        try:
            return bool(self._service.kill_job(job_id).killed)
        except self._unknown_job:
            return False

    def reconcile_jobs(self) -> None:
        self._service.reconcile()


class JobDirFiles:
    """Reads job files from the job service's ``jobs/`` directory."""

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
