"""Concrete adapters implementing the ports in ``behave_mcp.ports``."""

import json
import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from behave_mcp import behave_features, domain
from behave_mcp.messages import (
    Combo,
    Dimensions,
    DimensionValue,
    ExamplesBlock,
    FeatureCatalogEntry,
    FeatureDetail,
    ScenarioSummary,
)
from behave_mcp.ports import (
    Job,
    LogFileOpenError,
    ProcessStartError,
    ReservationResult,
)

logger = logging.getLogger(__name__)


class SubprocessHandle:
    """Wraps a Popen process together with its owned stdout log file."""

    def __init__(self, process: Any, log_file: Any) -> None:
        self._process = process
        self._log_file = log_file

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def close(self) -> None:
        if self._log_file is not None and not self._log_file.closed:
            self._log_file.close()

    def terminate(self) -> None:
        self._process.terminate()


class PopenLauncher:
    """Launches behave via ``subprocess.Popen`` writing to a log file."""

    def launch(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        stdout_log_path: Path,
    ) -> SubprocessHandle:
        try:
            log_file = stdout_log_path.open("w", encoding="utf-8")
        except OSError as exc:
            raise LogFileOpenError(str(exc)) from exc

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as exc:
            log_file.close()
            raise ProcessStartError(str(exc)) from exc

        return SubprocessHandle(process, log_file)

    def is_pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by another user/uid.
            return True
        except OSError as exc:
            logger.debug(
                "Unexpected OSError checking liveness of pid %s: %s", pid, exc
            )
            return False
        return True


class InMemoryJobRegistry:
    """Thread-safe in-memory registry of reserved and running behave jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def try_reserve(self, job: Job, max_parallel: int) -> ReservationResult:
        with self._lock:
            running_jobs = self._count_running_or_reserved_locked()
            if running_jobs >= max_parallel:
                return ReservationResult(
                    reserved=False,
                    running_jobs=running_jobs,
                    max_parallel=max_parallel,
                )
            self._jobs[job.job_id] = job
            return ReservationResult(
                reserved=True,
                running_jobs=running_jobs,
                max_parallel=max_parallel,
            )

    def register(self, job_id: str, job: Job) -> None:
        with self._lock:
            self._jobs[job_id] = job

    def release(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()

    def _count_running_or_reserved_locked(self) -> int:
        running_jobs = 0
        for job in self._jobs.values():
            if job.reserved:
                running_jobs += 1
                continue

            handle = job.process_handle
            if handle is None:
                continue

            if handle.poll() is None:
                running_jobs += 1

        return running_jobs


class LocalFeatureFileReader:
    """Filesystem-backed reader for the repository's feature file catalog.

    Delegates to ``behave_features``, translating its dataclasses into our
    own message DTOs so callers never depend on that package's shapes.
    """

    def discover_feature_files(self, repo_root: Path) -> list[str]:
        return behave_features.discover_feature_files(repo_root)

    def discover_feature_details(self, repo_root: Path) -> list[FeatureDetail]:
        return [
            _to_internal_feature_detail(detail)
            for detail in behave_features.discover_feature_details(repo_root)
        ]


class LocalFeatureCatalog:
    """Delegates pure catalog/filtering operations to ``behave_features``.

    Split from ``LocalFeatureFileReader`` because these are transformations
    over already-parsed data, not disk I/O -- same dependency, different
    kind of boundary. Every call converts our message DTOs to that
    package's dataclasses and back, so its shapes never leak past here.
    """

    def normalize_feature_file_arg(self, feature_file: str) -> str:
        return behave_features.normalize_feature_file_arg(feature_file)

    def catalog_entry(
        self, feature_detail: FeatureDetail
    ) -> FeatureCatalogEntry:
        external = behave_features.catalog_entry(
            _to_external_feature_detail(feature_detail)
        )
        return FeatureCatalogEntry(
            path=external.path,
            title=external.title,
            scenario_count=external.scenario_count,
            requires_config=list(external.requires_config),
            releases=list(external.releases),
            machine_types=list(external.machine_types),
        )

    def aggregate_dimensions(
        self, feature_details: list[FeatureDetail]
    ) -> Dimensions:
        external = behave_features.aggregate_dimensions(
            [_to_external_feature_detail(detail) for detail in feature_details]
        )
        return Dimensions(
            releases=[
                DimensionValue(
                    name=value.name, scenario_count=value.scenario_count
                )
                for value in external.releases
            ],
            machine_types=[
                DimensionValue(
                    name=value.name, scenario_count=value.scenario_count
                )
                for value in external.machine_types
            ],
        )

    def scenario_matches(
        self,
        scenario: ScenarioSummary,
        feature_tags: list[str],
        *,
        release: str | None = None,
        machine_type: str | None = None,
        tag: str | None = None,
        text: str | None = None,
    ) -> bool:
        return behave_features.scenario_matches(
            _to_external_scenario_summary(scenario),
            feature_tags,
            release=release,
            machine_type=machine_type,
            tag=tag,
            text=text,
        )

    def filtered_combos(
        self,
        scenario: ScenarioSummary,
        release: str | None = None,
        machine_type: str | None = None,
    ) -> list[Combo]:
        external_combos = behave_features.filtered_combos(
            _to_external_scenario_summary(scenario), release, machine_type
        )
        return [_to_internal_combo(combo) for combo in external_combos]


def _to_internal_combo(combo: Any) -> Combo:
    return Combo(release=combo.release, machine_type=combo.machine_type)


def _to_external_combo(combo: Combo) -> behave_features.Combo:
    return behave_features.Combo(
        release=combo.release, machine_type=combo.machine_type
    )


def _to_internal_examples_block(block: Any) -> ExamplesBlock:
    return ExamplesBlock(
        name=block.name,
        tags=list(block.tags),
        combos=[_to_internal_combo(combo) for combo in block.combos],
    )


def _to_external_examples_block(
    block: ExamplesBlock,
) -> behave_features.ExamplesBlock:
    return behave_features.ExamplesBlock(
        name=block.name,
        tags=list(block.tags),
        combos=[_to_external_combo(combo) for combo in block.combos],
    )


def _to_internal_scenario_summary(scenario: Any) -> ScenarioSummary:
    return ScenarioSummary(
        name=scenario.name,
        type=scenario.type,
        tags=list(scenario.tags),
        requires_config=list(scenario.requires_config),
        example_columns=list(scenario.example_columns),
        combos=[_to_internal_combo(combo) for combo in scenario.combos],
        examples=[
            _to_internal_examples_block(block) for block in scenario.examples
        ],
    )


def _to_external_scenario_summary(
    scenario: ScenarioSummary,
) -> behave_features.ScenarioSummary:
    return behave_features.ScenarioSummary(
        name=scenario.name,
        type=scenario.type,
        tags=list(scenario.tags),
        requires_config=list(scenario.requires_config),
        example_columns=list(scenario.example_columns),
        combos=[_to_external_combo(combo) for combo in scenario.combos],
        examples=[
            _to_external_examples_block(block) for block in scenario.examples
        ],
    )


def _to_internal_feature_detail(detail: Any) -> FeatureDetail:
    return FeatureDetail(
        path=detail.path,
        title=detail.title,
        tags=list(detail.tags),
        requires_config=list(detail.requires_config),
        scenarios=[
            _to_internal_scenario_summary(scenario)
            for scenario in detail.scenarios
        ],
    )


def _to_external_feature_detail(
    detail: FeatureDetail,
) -> behave_features.FeatureDetail:
    return behave_features.FeatureDetail(
        path=detail.path,
        title=detail.title,
        tags=list(detail.tags),
        requires_config=list(detail.requires_config),
        scenarios=[
            _to_external_scenario_summary(scenario)
            for scenario in detail.scenarios
        ],
    )


class LocalArtifactStore:
    """Filesystem-backed artifact persistence and log tailing."""

    def read_metadata(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_metadata(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            path.write_text(
                json.dumps(
                    payload, ensure_ascii=True, sort_keys=True, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            # Metadata write failures should not break job execution/status.
            return

    def append_index_event(self, log_dir: Path, event: dict[str, Any]) -> None:
        index_path = log_dir / domain.JOB_INDEX_FILE_NAME
        try:
            with index_path.open("a", encoding="utf-8") as index_stream:
                index_stream.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
                )
        except OSError:
            # Index write failures should not break job execution/status.
            return

    def tail_file(self, path: Path, lines: int) -> str:
        if not path.exists():
            return "Waiting for output..."

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return "".join(tail).rstrip() if tail else "Waiting for output..."

    def tail_lines(self, path: Path, lines: int) -> list[str]:
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return [line.rstrip("\n") for line in tail]

    def read_report_json(self, path: Path) -> list[Any] | None:
        if not path.exists():
            return None

        try:
            report_data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(report_data, list):
            return None

        return report_data

    def read_text_lines(self, path: Path) -> list[str] | None:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

    def exists(self, path: Path) -> bool:
        return path.exists()

    def list_job_ids(self, log_dir: Path) -> list[str]:
        if not log_dir.exists():
            return []
        suffix = "_meta.json"
        return sorted(
            path.name[: -len(suffix)] for path in log_dir.glob(f"*{suffix}")
        )


class LocalWorkspace:
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        if override:
            return self._validated_repo_root(Path(override).expanduser())

        env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
        if env_value:
            return self._validated_repo_root(Path(env_value).expanduser())

        detected = self._detect_repo_root(Path(__file__).resolve())
        if detected is not None:
            return detected

        raise ValueError(
            "Could not determine repo_root: not running from within a "
            "source checkout of ubuntu-pro-client (e.g. installed via "
            "'uvx --from'). Pass repo_root explicitly or set "
            "UBUNTU_PRO_CLIENT_REPO."
        )

    def _detect_repo_root(self, start: Path) -> Path | None:
        for candidate in [start, *start.parents]:
            if (candidate / "features").exists() and (
                candidate / "tox.ini"
            ).exists():
                return candidate.resolve()
        return None

    def resolve_log_dir(self, repo_root: Path) -> Path:
        env_path = os.environ.get("MCP_LOG_DIR")
        if env_path:
            log_dir = Path(env_path).resolve()
        else:
            log_dir = repo_root / ".mcp_behave_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def subprocess_env(self) -> dict[str, str]:
        return os.environ.copy()

    def _validated_repo_root(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        features_dir = resolved / "features"
        tox_file = resolved / "tox.ini"
        if not features_dir.exists() or not tox_file.exists():
            raise ValueError(
                "Invalid repo_root: expected directory containing "
                "features/ and tox.ini"
            )
        return resolved
