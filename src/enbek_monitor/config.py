"""Local configuration loading and safe source-profile selection.

The configuration layer is deliberately local-only.  It reads a JSON/TOML
file (or an already supplied mapping), validates all public settings before a
run can start, and delegates source-profile parsing to
:mod:`enbek_monitor.source_discovery`.  No URL is contacted here and no
filesystem artifact is created by this module.

A configured workspace is the root for every configured artifact path.  The
config file itself and an explicitly supplied in-memory profile are read-only
inputs; profile, fixture and storage paths are still checked against the
workspace before they are used.
"""

from __future__ import annotations

import copy
import json
import math
import re
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeAlias

from .errors import MonitorError
from .source_discovery import (
    CaptureValidationError,
    SourceProfile,
    SourceUnavailableError,
    discover_source,
)


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
Transport: TypeAlias = Literal["auto", "http", "fixture"]
RunMode: TypeAlias = Literal["bootstrap", "sync", "dry_run"]

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAYS: tuple[float, float] = (2.0, 5.0)
DEFAULT_MAX_CONCURRENCY = 5
DEFAULT_USER_AGENT = "enbek-professional-standards-monitor/0.1"
DEFAULT_PDF_MIN_BYTES = 1024
DEFAULT_ANOMALY_THRESHOLD = 0.30
DEFAULT_STORAGE_DIRECTORIES: dict[str, str] = {
    "raw_dir": "raw",
    "data_dir": "data",
    "history_dir": "history",
    "docs_dir": "docs",
    "work_dir": "work",
}
DEFAULT_LOCALE = "ru"
DEFAULT_DATE_FORMAT = "YYYY-MM-DD"
DEFAULT_SLUG_POLICY = "enbek_id"

_CONFIG_FILE_SUFFIXES = frozenset({".json", ".toml"})
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "workspace",
        "source",
        "network",
        "validation",
        "storage",
        "presentation",
        # Explicit aliases are useful for a small CLI config and are merged
        # into ``source`` before the source profile is selected.
        "source_profile",
        "source_profile_path",
        "capture",
        "capture_path",
        "fixture_profile",
        "fixture_profile_path",
        "source_transport",
    }
)
_ALLOWED_WORKSPACE = frozenset({"root", "path"})
_ALLOWED_SOURCE = frozenset(
    {
        "transport",
        "mode",
        "profile",
        "profile_path",
        "source_profile",
        "source_profile_path",
        "capture",
        "capture_path",
        "fixture_profile",
        "fixture_profile_path",
        "fixture_transport",
        "registry_url",
    }
)
_ALLOWED_NETWORK = frozenset(
    {
        "timeout",
        "timeout_seconds",
        "max_attempts",
        "retry_delays",
        "delays",
        "max_concurrency",
        "concurrency",
        "user_agent",
    }
)
_ALLOWED_VALIDATION = frozenset({"pdf_min_bytes", "anomaly_threshold"})
_ALLOWED_STORAGE = frozenset(
    {
        "raw",
        "raw_dir",
        "data",
        "data_dir",
        "history",
        "history_dir",
        "docs",
        "docs_dir",
        "work",
        "work_dir",
    }
)
_ALLOWED_PRESENTATION = frozenset({"locale", "date_format", "slug_policy"})

# A deny-list is intentionally applied recursively to the configuration
# document.  Source_Profile has its own public-header allow-list; this second
# check prevents credentials from entering through unrelated config sections.
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "tokens",
        "cookie",
        "cookies",
        "secret",
        "secrets",
        "password",
        "passwd",
        "credential",
        "credentials",
        "github",
        "remote",
        "deployment",
        "deploy",
        "database",
        "postgresql",
        "postgres",
        "neo4j",
        "vector_database",
        "vector_db",
        "llm",
        "frontend",
        "notion",
        "push",
        "commit",
    }
)
_FORBIDDEN_KEY_RE = re.compile(
    r"(?:^|[_ .-])(authorization|api[_ .-]?key|access[_ .-]?token|"
    r"refresh[_ .-]?token|id[_ .-]?token|cookie|cookies|secret|password|"
    r"passwd|credential|credentials|github|remote|deployment|deploy|"
    r"database|postgres(?:ql)?|neo4j|vector[_ .-]?db|llm|frontend|notion|"
    r"push|commit)(?:$|[_ .-])",
    re.IGNORECASE,
)
_PATH_KEY_PARTS = frozenset(
    {
        "workspace",
        "root",
        "path",
        "profile",
        "profile_path",
        "source_profile",
        "source_profile_path",
        "capture",
        "capture_path",
        "fixture_profile",
        "fixture_profile_path",
        "fixture_root",
        "registry_pages",
        "registry_page_paths",
        "details",
        "detail_paths",
        "pdfs",
        "pdf_response_paths",
        "normalized_record_paths",
        "raw",
        "raw_dir",
        "data",
        "data_dir",
        "history",
        "history_dir",
        "docs",
        "docs_dir",
        "work",
        "work_dir",
    }
)
_PATH_MAPPING_KEYS = frozenset(
    {
        "details",
        "detail_paths",
        "pdfs",
        "pdf_response_paths",
    }
)
_URL_KEY_PARTS = frozenset(
    {
        "url",
        "urls",
        "registry_url",
        "page_url",
        "pdf_url",
        "detail_url",
        "source_url",
    }
)


class ConfigError(MonitorError):
    """Structured error raised before a monitor run can start."""

    error_type = "ConfigError"
    default_stage = "configuration"

    def __init__(
        self,
        reason: str,
        *,
        path: str | None = "$",
        config_path: str | Path | None = None,
        workspace: str | Path | None = None,
        missing_fields: Sequence[str] | None = None,
        invalid_fields: Sequence[str] | None = None,
    ) -> None:
        self.config_path = None if config_path is None else str(config_path)
        self.workspace = None if workspace is None else str(workspace)
        super().__init__(
            reason,
            stage="configuration",
            path=path,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )

    def _extra_payload(self) -> dict[str, JSONValue]:
        payload = super()._extra_payload()
        payload.update(
            {
                "config_path": self.config_path,
                "workspace": self.workspace,
            }
        )
        return payload


# Friendly aliases used by callers that prefer a longer name.
ConfigValidationError = ConfigError
ConfigurationError = ConfigError


def _path_text(path: Sequence[object]) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _copy_value(value: Any, path: Sequence[object] = ()) -> Any:
    """Copy config values and reject values that cannot be represented safely."""

    if value is None or isinstance(value, (str, bool, int, Path)):
        return copy.deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(
                "configuration contains a non-finite number",
                path=_path_text(path),
                invalid_fields=(_path_text(path),),
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ConfigError(
                    "configuration object keys must be non-empty strings",
                    path=_path_text(path),
                    invalid_fields=(_path_text(path),),
                )
            result[raw_key] = _copy_value(item, (*path, raw_key))
        return result
    if isinstance(value, (list, tuple)):
        return [_copy_value(item, (*path, index)) for index, item in enumerate(value)]
    # TOML dates/times are not useful configuration values in this MVP.  They
    # are rejected rather than converted implicitly or serialized ambiguously.
    raise ConfigError(
        f"unsupported configuration value type: {type(value).__name__}",
        path=_path_text(path),
        invalid_fields=(_path_text(path),),
    )


def _required_text(value: Any, path: Sequence[object]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{_path_text(path)} must be a non-empty string",
            path=_path_text(path),
            invalid_fields=(_path_text(path),),
        )
    return value.strip()


def _as_mapping(value: Any, path: Sequence[object]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"{_path_text(path)} must be an object/table",
            path=_path_text(path),
            invalid_fields=(_path_text(path),),
        )
    return dict(value)


def _check_allowed_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    path: Sequence[object],
) -> None:
    unknown = [key for key in value if key not in allowed]
    if unknown:
        paths = tuple(_path_text((*path, key)) for key in unknown)
        raise ConfigError(
            "configuration contains unsupported field(s): " + ", ".join(paths),
            path=paths[0],
            invalid_fields=paths,
        )


def _normalised_key(value: str) -> str:
    return re.sub(r"[- .]+", "_", value.strip().lower())


def _is_forbidden_key(key: str) -> bool:
    normalized = _normalised_key(key)
    if normalized in _FORBIDDEN_KEY_PARTS:
        return True
    return bool(_FORBIDDEN_KEY_RE.search(key))


def _reject_forbidden_fields(value: Any, path: Sequence[object] = ()) -> None:
    """Reject credentials and remote/deployment controls before interpretation."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            current = (*path, key)
            if _is_forbidden_key(key):
                location = _path_text(current)
                raise ConfigError(
                    f"forbidden credential, remote or deployment field at {location}",
                    path=location,
                    invalid_fields=(location,),
                )
            _reject_forbidden_fields(item, current)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden_fields(item, (*path, index))


def _looks_like_url_key(key: str) -> bool:
    normalized = _normalised_key(key)
    return normalized in _URL_KEY_PARTS or normalized.endswith("_url")


def _path_key(key: str) -> bool:
    normalized = _normalised_key(key)
    return normalized in _PATH_KEY_PARTS or normalized.endswith("_path")


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_path(
    value: Any,
    *,
    root: Path,
    path: Sequence[object],
    base: Path | None = None,
    allow_root: bool = True,
) -> Path:
    """Resolve one configured path and require it to remain under root."""

    if not isinstance(value, (str, Path)):
        raise ConfigError(
            f"{_path_text(path)} must be a path string",
            path=_path_text(path),
            workspace=root,
            invalid_fields=(_path_text(path),),
        )
    text = str(value)
    if not text.strip():
        raise ConfigError(
            f"{_path_text(path)} must not be empty",
            path=_path_text(path),
            workspace=root,
            invalid_fields=(_path_text(path),),
        )
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    resolved = candidate.expanduser().resolve(strict=False)
    if not _is_within(root, resolved) or (not allow_root and resolved == root):
        raise ConfigError(
            f"configured path is outside Local_Workspace: {text!r}",
            path=_path_text(path),
            workspace=root,
            invalid_fields=(_path_text(path),),
        )
    return resolved


def _validate_path_values(
    value: Any,
    *,
    root: Path,
    path: Sequence[object] = (),
    key_hint: str | None = None,
) -> None:
    """Walk path-bearing config fields, including fixture maps and lists."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            current = (*path, key)
            if _looks_like_url_key(key):
                # URLs are source values, not local filesystem paths.
                if isinstance(item, (Mapping, list, tuple)):
                    _validate_path_values(item, root=root, path=current, key_hint=key)
                continue
            if _path_key(key):
                normalized_key = _normalised_key(key)
                if isinstance(item, Mapping):
                    for child_key, child_value in item.items():
                        child_path = (*current, str(child_key))
                        if normalized_key in _PATH_MAPPING_KEYS and not isinstance(
                            child_value, (Mapping, list, tuple)
                        ):
                            _resolve_path(child_value, root=root, path=child_path)
                        else:
                            _validate_path_values(
                                child_value,
                                root=root,
                                path=child_path,
                                key_hint=str(child_key),
                            )
                elif isinstance(item, (list, tuple)):
                    for index, child_value in enumerate(item):
                        _resolve_path(
                            child_value,
                            root=root,
                            path=(*current, index),
                        )
                elif item is not None and not isinstance(item, bool):
                    _resolve_path(item, root=root, path=current)
                continue
            _validate_path_values(item, root=root, path=current, key_hint=key)
        return
    if isinstance(value, (list, tuple)):
        if key_hint and _path_key(key_hint):
            for index, item in enumerate(value):
                _resolve_path(item, root=root, path=(*path, index))
        else:
            for index, item in enumerate(value):
                _validate_path_values(item, root=root, path=(*path, index), key_hint=key_hint)


@dataclass(frozen=True)
class NetworkConfig:
    """Network policy values consumed by ``Network_Client``."""

    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_delays: tuple[float, float] = DEFAULT_RETRY_DELAYS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def timeout_seconds(self) -> float:
        return self.timeout

    @property
    def delays(self) -> list[float]:
        """Return JSON/config-friendly retry delays as a defensive list copy."""

        return list(self.retry_delays)

    @property
    def concurrency(self) -> int:
        return self.max_concurrency

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "timeout": self.timeout,
            "max_attempts": self.max_attempts,
            "retry_delays": list(self.retry_delays),
            "max_concurrency": self.max_concurrency,
            "user_agent": self.user_agent,
        }


@dataclass(frozen=True)
class ValidationConfig:
    """Candidate validation thresholds fixed by the MVP contract."""

    pdf_min_bytes: int = DEFAULT_PDF_MIN_BYTES
    anomaly_threshold: float = DEFAULT_ANOMALY_THRESHOLD

    @property
    def anomaly_fraction(self) -> float:
        return self.anomaly_threshold

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "pdf_min_bytes": self.pdf_min_bytes,
            "anomaly_threshold": self.anomaly_threshold,
        }


@dataclass(frozen=True)
class StorageConfig:
    """Relative artifact directory names, all rooted in Local_Workspace."""

    raw_dir: str = DEFAULT_STORAGE_DIRECTORIES["raw_dir"]
    data_dir: str = DEFAULT_STORAGE_DIRECTORIES["data_dir"]
    history_dir: str = DEFAULT_STORAGE_DIRECTORIES["history_dir"]
    docs_dir: str = DEFAULT_STORAGE_DIRECTORIES["docs_dir"]
    work_dir: str = DEFAULT_STORAGE_DIRECTORIES["work_dir"]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "raw_dir": self.raw_dir,
            "data_dir": self.data_dir,
            "history_dir": self.history_dir,
            "docs_dir": self.docs_dir,
            "work_dir": self.work_dir,
        }


@dataclass(frozen=True)
class PresentationConfig:
    """Deterministic local Markdown presentation settings."""

    locale: str = DEFAULT_LOCALE
    date_format: str = DEFAULT_DATE_FORMAT
    slug_policy: str = DEFAULT_SLUG_POLICY

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "locale": self.locale,
            "date_format": self.date_format,
            "slug_policy": self.slug_policy,
        }


@dataclass(frozen=True)
class SourceConfig:
    """Selected source profile and the transport mode used by a run."""

    profile: SourceProfile
    transport: Literal["http", "fixture"]
    requested_transport: Transport = "auto"
    profile_path: Path | None = None
    capture_path: Path | None = None
    fixture_profile_path: Path | None = None
    fixture_transport: Mapping[str, Any] = field(default_factory=dict)

    @property
    def registry_url(self) -> str:
        """The exact URL recorded by the selected Source_Profile."""

        return self.profile.registry_url

    @property
    def parser_mode(self) -> str:
        return self.profile.parser_mode

    @property
    def is_fixture(self) -> bool:
        return self.transport == "fixture" or self.profile.is_fixture

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "transport": self.transport,
            "requested_transport": self.requested_transport,
            "profile": self.profile.to_dict(),
        }
        for name, value in (
            ("profile_path", self.profile_path),
            ("capture_path", self.capture_path),
            ("fixture_profile_path", self.fixture_profile_path),
        ):
            if value is not None:
                result[name] = value.as_posix()
        if self.fixture_transport:
            result["fixture_transport"] = _copy_value(self.fixture_transport)
        return result


@dataclass(frozen=True)
class RunContext:
    """Immutable metadata passed from configuration into one local run."""

    run_id: str
    started_at: datetime
    workspace: Path
    registry_url: str
    mode: RunMode
    staging_root: Path
    source_profile: SourceProfile

    @property
    def started_at_utc(self) -> str:
        return self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def exact_registry_url(self) -> str:
        return self.registry_url

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at_utc,
            "workspace": self.workspace.as_posix(),
            "registry_url": self.registry_url,
            "mode": self.mode,
            "staging_root": self.staging_root.as_posix(),
            "source_profile": self.source_profile.to_dict(),
        }


@dataclass(frozen=True)
class MonitorConfig:
    """Fully validated local monitor configuration."""

    workspace: Path
    source: SourceConfig
    network: NetworkConfig = field(default_factory=NetworkConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    presentation: PresentationConfig = field(default_factory=PresentationConfig)
    config_path: Path | None = None

    @property
    def registry_url(self) -> str:
        return self.source.registry_url

    @property
    def exact_registry_url(self) -> str:
        return self.source.registry_url

    @property
    def source_profile(self) -> SourceProfile:
        return self.source.profile

    @property
    def storage_paths(self) -> dict[str, Path]:
        return {
            name: (self.workspace / relative).resolve(strict=False)
            for name, relative in self.storage.to_dict().items()
        }

    def path_for(self, relative_path: str | Path) -> Path:
        """Resolve an additional artifact path under the configured workspace."""

        return _resolve_path(
            relative_path,
            root=self.workspace,
            path=("path",),
        )

    def create_run_context(
        self,
        mode: RunMode = "sync",
        *,
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        if mode not in {"bootstrap", "sync", "dry_run"}:
            raise ConfigError(
                f"unsupported run mode: {mode!r}",
                path="$.mode",
                config_path=self.config_path,
                workspace=self.workspace,
                invalid_fields=("$.mode",),
            )
        started_at = _utc_second(now or datetime.now(timezone.utc))
        if run_id is None:
            run_id = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
        if not isinstance(run_id, str) or not run_id.strip() or "/" in run_id or "\\" in run_id:
            raise ConfigError(
                "run_id must be a non-empty path-safe string",
                path="$.run_id",
                config_path=self.config_path,
                workspace=self.workspace,
                invalid_fields=("$.run_id",),
            )
        staging_root = self.workspace / self.storage.work_dir / "runs" / run_id
        staging_root = staging_root.resolve(strict=False)
        if not _is_within(self.workspace, staging_root):
            raise ConfigError(
                "run staging path is outside Local_Workspace",
                path="$.storage.work_dir",
                config_path=self.config_path,
                workspace=self.workspace,
                invalid_fields=("$.storage.work_dir",),
            )
        return RunContext(
            run_id=run_id,
            started_at=started_at,
            workspace=self.workspace,
            registry_url=self.source.registry_url,
            mode=mode,
            staging_root=staging_root,
            source_profile=self.source.profile,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "workspace": {"root": self.workspace.as_posix()},
            "source": self.source.to_dict(),
            "network": self.network.to_dict(),
            "validation": self.validation.to_dict(),
            "storage": self.storage.to_dict(),
            "presentation": self.presentation.to_dict(),
        }
        if self.config_path is not None:
            result["config_path"] = self.config_path.as_posix()
        return result


# Names used by downstream code and by the design vocabulary.
Config = MonitorConfig
LocalMonitorConfig = MonitorConfig
Source_Profile = SourceProfile


def _utc_second(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigError("run timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _read_config_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"configuration file does not exist: {path}",
            path="$",
            config_path=path,
            invalid_fields=("$",),
        )
    if not path.is_file():
        raise ConfigError(
            f"configuration path is not a file: {path}",
            path="$",
            config_path=path,
            invalid_fields=("$",),
        )
    suffix = path.suffix.lower()
    if suffix not in _CONFIG_FILE_SUFFIXES:
        raise ConfigError(
            "configuration file must use .json or .toml",
            path="$",
            config_path=path,
            invalid_fields=("$",),
        )
    try:
        text = path.read_text(encoding="utf-8")
        if suffix == ".json":
            value = json.loads(text)
        else:
            value = tomllib.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"could not parse local configuration: {exc}",
            path="$",
            config_path=path,
            invalid_fields=("$",),
        ) from exc
    if not isinstance(value, Mapping):
        raise ConfigError(
            "top-level configuration must be an object/table",
            path="$",
            config_path=path,
            invalid_fields=("$",),
        )
    return dict(value)


def _resolve_workspace_root(
    raw: Mapping[str, Any],
    *,
    explicit_workspace: str | Path | None,
    config_path: Path | None,
) -> Path:
    workspace_value: Any = raw.get("workspace")
    workspace_path_value: Any = None
    if isinstance(workspace_value, Mapping):
        _check_allowed_keys(workspace_value, _ALLOWED_WORKSPACE, ("workspace",))
        workspace_path_value = workspace_value.get("root", workspace_value.get("path"))
    elif workspace_value is not None:
        workspace_path_value = workspace_value
    if explicit_workspace is not None:
        explicit_base = Path.cwd()
        if not isinstance(explicit_workspace, (str, Path)) or not str(explicit_workspace).strip():
            raise ConfigError(
                "workspace must be a non-empty path string",
                path="$.workspace",
                config_path=config_path,
                invalid_fields=("$.workspace",),
            )
        explicit_candidate = Path(str(explicit_workspace)).expanduser()
        if not explicit_candidate.is_absolute():
            explicit_candidate = explicit_base / explicit_candidate
        explicit_root = explicit_candidate.resolve(strict=False)
        if workspace_path_value is not None:
            config_base = config_path.parent if config_path is not None else Path.cwd()
            if not isinstance(workspace_path_value, (str, Path)) or not str(workspace_path_value).strip():
                raise ConfigError(
                    "workspace.root must be a non-empty path string",
                    path="$.workspace.root",
                    config_path=config_path,
                    workspace=explicit_root,
                    invalid_fields=("$.workspace.root",),
                )
            configured_candidate = Path(str(workspace_path_value)).expanduser()
            if not configured_candidate.is_absolute():
                configured_candidate = config_base / configured_candidate
            configured_root = configured_candidate.resolve(strict=False)
            if configured_root != explicit_root:
                raise ConfigError(
                    "explicit --workspace conflicts with workspace.root in configuration",
                    path="$.workspace",
                    config_path=config_path,
                    workspace=explicit_root,
                    invalid_fields=("$.workspace",),
                )
        return explicit_root
    if workspace_path_value is None:
        raise ConfigError(
            "Local_Workspace must be supplied explicitly via --workspace or workspace.root",
            path="$.workspace",
            config_path=config_path,
            invalid_fields=("$.workspace",),
        )
    base = config_path.parent if config_path is not None else Path.cwd()
    candidate = Path(str(workspace_path_value)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    return _as_mapping(raw.get(name), (name,))


def _parse_network(raw: Mapping[str, Any], *, config_path: Path | None) -> NetworkConfig:
    section = _section(raw, "network")
    _check_allowed_keys(section, _ALLOWED_NETWORK, ("network",))
    timeout_value = section.get("timeout", section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise ConfigError(
            "network.timeout must be a positive number",
            path="$.network.timeout",
            config_path=config_path,
            invalid_fields=("$.network.timeout",),
        )
    timeout = float(timeout_value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError(
            "network.timeout must be a positive finite number",
            path="$.network.timeout",
            config_path=config_path,
            invalid_fields=("$.network.timeout",),
        )

    attempts_value = section.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if isinstance(attempts_value, bool) or not isinstance(attempts_value, int) or not 1 <= attempts_value <= 3:
        raise ConfigError(
            "network.max_attempts must be an integer from 1 through 3",
            path="$.network.max_attempts",
            config_path=config_path,
            invalid_fields=("$.network.max_attempts",),
        )

    delays_value = section.get("retry_delays", section.get("delays", DEFAULT_RETRY_DELAYS))
    if not isinstance(delays_value, (list, tuple)) or len(delays_value) != 2:
        raise ConfigError(
            "network.retry_delays must contain exactly two non-negative numbers",
            path="$.network.retry_delays",
            config_path=config_path,
            invalid_fields=("$.network.retry_delays",),
        )
    delays: list[float] = []
    for index, item in enumerate(delays_value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(
                "network.retry_delays must contain numbers",
                path=f"$.network.retry_delays[{index}]",
                config_path=config_path,
                invalid_fields=(f"$.network.retry_delays[{index}]",),
            )
        number = float(item)
        if not math.isfinite(number) or number < 0:
            raise ConfigError(
                "network.retry_delays must contain finite non-negative numbers",
                path=f"$.network.retry_delays[{index}]",
                config_path=config_path,
                invalid_fields=(f"$.network.retry_delays[{index}]",),
            )
        delays.append(number)

    concurrency_value = section.get("max_concurrency", section.get("concurrency", DEFAULT_MAX_CONCURRENCY))
    if (
        isinstance(concurrency_value, bool)
        or not isinstance(concurrency_value, int)
        or not 1 <= concurrency_value <= DEFAULT_MAX_CONCURRENCY
    ):
        raise ConfigError(
            "network.max_concurrency must be an integer from 1 through 5",
            path="$.network.max_concurrency",
            config_path=config_path,
            invalid_fields=("$.network.max_concurrency",),
        )
    user_agent = _required_text(section.get("user_agent", DEFAULT_USER_AGENT), ("network", "user_agent"))
    return NetworkConfig(
        timeout=timeout,
        max_attempts=attempts_value,
        retry_delays=(delays[0], delays[1]),
        max_concurrency=concurrency_value,
        user_agent=user_agent,
    )


def _parse_validation(raw: Mapping[str, Any], *, config_path: Path | None) -> ValidationConfig:
    section = _section(raw, "validation")
    _check_allowed_keys(section, _ALLOWED_VALIDATION, ("validation",))
    pdf_min = section.get("pdf_min_bytes", DEFAULT_PDF_MIN_BYTES)
    if isinstance(pdf_min, bool) or not isinstance(pdf_min, int) or pdf_min < DEFAULT_PDF_MIN_BYTES:
        raise ConfigError(
            "validation.pdf_min_bytes must be an integer of at least 1024",
            path="$.validation.pdf_min_bytes",
            config_path=config_path,
            invalid_fields=("$.validation.pdf_min_bytes",),
        )
    threshold_value = section.get("anomaly_threshold", DEFAULT_ANOMALY_THRESHOLD)
    if isinstance(threshold_value, bool) or not isinstance(threshold_value, (int, float)):
        raise ConfigError(
            "validation.anomaly_threshold must be a number from 0 through 1",
            path="$.validation.anomaly_threshold",
            config_path=config_path,
            invalid_fields=("$.validation.anomaly_threshold",),
        )
    threshold = float(threshold_value)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ConfigError(
            "validation.anomaly_threshold must be a finite number from 0 through 1",
            path="$.validation.anomaly_threshold",
            config_path=config_path,
            invalid_fields=("$.validation.anomaly_threshold",),
        )
    # The MVP validation rule is specifically a decrease greater than 30%.
    # Keeping this value fixed prevents a config file from silently weakening
    # the safe-current guarantee.
    if not math.isclose(threshold, DEFAULT_ANOMALY_THRESHOLD, rel_tol=0, abs_tol=1e-12):
        raise ConfigError(
            "validation.anomaly_threshold must remain 0.30 for the MVP",
            path="$.validation.anomaly_threshold",
            config_path=config_path,
            invalid_fields=("$.validation.anomaly_threshold",),
        )
    return ValidationConfig(pdf_min_bytes=pdf_min, anomaly_threshold=threshold)


def _storage_value(
    section: Mapping[str, Any],
    names: tuple[str, ...],
    default: str,
    *,
    workspace: Path,
    config_path: Path | None,
    field_name: str,
) -> str:
    value: Any = None
    for name in names:
        if name in section:
            value = section[name]
            break
    if value is None:
        value = default
    resolved = _resolve_path(
        value,
        root=workspace,
        path=("storage", field_name),
        allow_root=False,
    )
    return resolved.relative_to(workspace).as_posix()


def _parse_storage(
    raw: Mapping[str, Any],
    *,
    workspace: Path,
    config_path: Path | None,
) -> StorageConfig:
    section = _section(raw, "storage")
    _check_allowed_keys(section, _ALLOWED_STORAGE, ("storage",))
    return StorageConfig(
        raw_dir=_storage_value(section, ("raw_dir", "raw"), "raw", workspace=workspace, config_path=config_path, field_name="raw_dir"),
        data_dir=_storage_value(section, ("data_dir", "data"), "data", workspace=workspace, config_path=config_path, field_name="data_dir"),
        history_dir=_storage_value(section, ("history_dir", "history"), "history", workspace=workspace, config_path=config_path, field_name="history_dir"),
        docs_dir=_storage_value(section, ("docs_dir", "docs"), "docs", workspace=workspace, config_path=config_path, field_name="docs_dir"),
        work_dir=_storage_value(section, ("work_dir", "work"), "work", workspace=workspace, config_path=config_path, field_name="work_dir"),
    )


def _parse_presentation(raw: Mapping[str, Any], *, config_path: Path | None) -> PresentationConfig:
    section = _section(raw, "presentation")
    _check_allowed_keys(section, _ALLOWED_PRESENTATION, ("presentation",))
    locale = _required_text(section.get("locale", DEFAULT_LOCALE), ("presentation", "locale"))
    date_format = _required_text(section.get("date_format", DEFAULT_DATE_FORMAT), ("presentation", "date_format"))
    if date_format != DEFAULT_DATE_FORMAT:
        raise ConfigError(
            "presentation.date_format must remain YYYY-MM-DD for the MVP",
            path="$.presentation.date_format",
            config_path=config_path,
            invalid_fields=("$.presentation.date_format",),
        )
    slug_policy = _required_text(section.get("slug_policy", DEFAULT_SLUG_POLICY), ("presentation", "slug_policy"))
    return PresentationConfig(locale=locale, date_format=date_format, slug_policy=slug_policy)


def _path_if_present(
    value: Any,
    *,
    root: Path,
    path: Sequence[object],
) -> Path | None:
    if value is None:
        return None
    return _resolve_path(value, root=root, path=path)


def _profile_input(
    value: Any,
    *,
    root: Path,
    path: Sequence[object],
) -> tuple[Any, Path | None]:
    if value is None:
        return None, None
    if isinstance(value, (str, Path)):
        resolved = _resolve_path(value, root=root, path=path)
        return resolved, resolved
    if isinstance(value, Mapping) or isinstance(value, SourceProfile):
        return value, None
    raise ConfigError(
        f"{_path_text(path)} must be a local profile path or object",
        path=_path_text(path),
        workspace=root,
        invalid_fields=(_path_text(path),),
    )


def _extract_profile_fixture_transport(value: Any) -> Mapping[str, Any]:
    """Read optional fixture transport metadata without making network calls."""

    document: Any = value
    if isinstance(value, SourceProfile):
        return {}
    if isinstance(value, (str, Path)):
        try:
            document = json.loads(Path(value).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
    if isinstance(document, Mapping):
        fixture_transport = document.get("fixture_transport", {})
        if isinstance(fixture_transport, Mapping):
            return fixture_transport
    return {}


def _merge_mappings(*values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        for key, item in value.items():
            if isinstance(result.get(key), Mapping) and isinstance(item, Mapping):
                result[key] = _merge_mappings(result[key], item)
            else:
                result[key] = copy.deepcopy(item)
    return result


def _source_section(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = _section(raw, "source")
    aliases = {
        "source_profile": "profile",
        "source_profile_path": "profile_path",
        "capture": "capture",
        "capture_path": "capture_path",
        "fixture_profile": "fixture_profile",
        "fixture_profile_path": "fixture_profile_path",
        "source_transport": "transport",
    }
    merged = dict(source)
    for source_key, section_key in aliases.items():
        if source_key in raw:
            if section_key in merged and merged[section_key] != raw[source_key]:
                raise ConfigError(
                    f"conflicting source configuration fields: {source_key} and source.{section_key}",
                    path=f"$.{source_key}",
                    invalid_fields=(f"$.{source_key}", f"$.source.{section_key}"),
                )
            merged[section_key] = raw[source_key]
    return merged


def _parse_source(
    raw: Mapping[str, Any],
    *,
    workspace: Path,
    config_path: Path | None,
    source_profile_override: Any = None,
    fixture_profile_override: Any = None,
    transport_override: Transport | None = None,
) -> SourceConfig:
    section = _source_section(raw)
    _check_allowed_keys(section, _ALLOWED_SOURCE, ("source",))

    requested_raw = transport_override if transport_override is not None else section.get(
        "transport", section.get("mode", "auto")
    )
    if requested_raw not in {"auto", "http", "fixture"}:
        raise ConfigError(
            "source.transport must be auto, http, or fixture",
            path="$.source.transport",
            config_path=config_path,
            workspace=workspace,
            invalid_fields=("$.source.transport",),
        )
    requested_transport: Transport = requested_raw  # type: ignore[assignment]

    profile_value = source_profile_override
    if profile_value is None:
        profile_value = section.get(
            "profile",
            section.get("profile_path", section.get("source_profile", section.get("source_profile_path"))),
        )
    capture_value = section.get("capture", section.get("capture_path"))
    fixture_value = fixture_profile_override
    if fixture_value is None:
        fixture_value = section.get("fixture_profile", section.get("fixture_profile_path"))

    profile_input, profile_path = _profile_input(
        profile_value,
        root=workspace,
        path=("source", "profile"),
    )
    capture_input, capture_path = _profile_input(
        capture_value,
        root=workspace,
        path=("source", "capture"),
    )
    fixture_input, fixture_profile_path = _profile_input(
        fixture_value,
        root=workspace,
        path=("source", "fixture_profile"),
    )

    # A generic profile reference is interpreted according to an explicit
    # fixture selection, otherwise it is offered to discovery as a capture.
    if profile_input is not None:
        if requested_transport == "fixture":
            if fixture_input is not None:
                raise ConfigError(
                    "source.profile and source.fixture_profile cannot both be used with fixture transport",
                    path="$.source.profile",
                    workspace=workspace,
                    config_path=config_path,
                    invalid_fields=("$.source.profile", "$.source.fixture_profile"),
                )
            fixture_input = profile_input
            fixture_profile_path = profile_path
        elif capture_input is None:
            capture_input = profile_input
            capture_path = profile_path

    if requested_transport == "fixture" and fixture_input is None:
        raise ConfigError(
            "fixture transport requires source.profile or source.fixture_profile",
            path="$.source.fixture_profile",
            config_path=config_path,
            workspace=workspace,
            missing_fields=("$.source.fixture_profile",),
        )

    try:
        if requested_transport == "fixture":
            profile = discover_source(fixture_profile=fixture_input)
        else:
            profile = discover_source(capture=capture_input, fixture_profile=fixture_input)
    except (CaptureValidationError, SourceUnavailableError, FileNotFoundError) as exc:
        violations = tuple(getattr(exc, "violations", ()))
        path = violations[0] if violations else "$.source"
        raise ConfigError(
            f"invalid or unavailable Source_Profile: {exc}",
            path=path,
            config_path=config_path,
            workspace=workspace,
            invalid_fields=violations or ("$.source",),
        ) from exc

    configured_url = section.get("registry_url")
    if configured_url is not None:
        configured_url = _required_text(configured_url, ("source", "registry_url"))
        if configured_url != profile.registry_url:
            raise ConfigError(
                "source.registry_url must exactly match the selected Source_Profile registry URL",
                path="$.source.registry_url",
                config_path=config_path,
                workspace=workspace,
                invalid_fields=("$.source.registry_url",),
            )

    configured_fixture_transport = section.get("fixture_transport", {})
    if configured_fixture_transport is None:
        configured_fixture_transport = {}
    if not isinstance(configured_fixture_transport, Mapping):
        raise ConfigError(
            "source.fixture_transport must be an object/table",
            path="$.source.fixture_transport",
            config_path=config_path,
            workspace=workspace,
            invalid_fields=("$.source.fixture_transport",),
        )
    profile_fixture_transport = _merge_mappings(
        _extract_profile_fixture_transport(profile_input),
        _extract_profile_fixture_transport(capture_input),
        _extract_profile_fixture_transport(fixture_input),
    )
    fixture_transport = _merge_mappings(profile_fixture_transport, configured_fixture_transport)
    _validate_path_values(
        {"fixture_transport": fixture_transport},
        root=workspace,
        path=("source",),
    )
    fixture_transport_copy = _copy_value(fixture_transport, ("source", "fixture_transport"))
    return SourceConfig(
        profile=profile,
        transport="fixture" if profile.is_fixture else "http",
        requested_transport=requested_transport,
        profile_path=profile_path,
        capture_path=capture_path,
        fixture_profile_path=fixture_profile_path,
        fixture_transport=fixture_transport_copy,
    )


class ConfigLoader:
    """Load and validate one local monitor configuration.

    ``workspace`` is the programmatic equivalent of ``--workspace``.  It is
    intentionally explicit: when neither it nor ``workspace.root`` in the
    local config is present, loading fails instead of falling back to the
    process current directory.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        source_profile: Any = None,
        fixture_profile: Any = None,
        source_transport: Transport | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        if config_path is not None and config is not None:
            raise ConfigError(
                "provide either config_path or config mapping, not both",
                path="$",
                invalid_fields=("$.config_path", "$.config"),
            )
        self.config_path = None if config_path is None else Path(config_path).expanduser().resolve(strict=False)
        self.workspace_override = workspace
        self.source_profile_override = source_profile
        self.fixture_profile_override = fixture_profile
        self.source_transport_override = source_transport
        self.config_mapping = None if config is None else dict(config)

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> "ConfigLoader":
        return cls(path, **overrides)

    def load(
        self,
        *,
        workspace: str | Path | None = None,
        source_profile: Any = None,
        fixture_profile: Any = None,
        source_transport: Transport | None = None,
    ) -> MonitorConfig:
        """Read, validate and return a configuration without starting a run."""

        config_path = self.config_path
        if self.config_mapping is not None:
            raw = _copy_value(self.config_mapping, ())
            if not isinstance(raw, Mapping):  # defensive; constructor accepts Mapping
                raise ConfigError("configuration must be an object/table", path="$", invalid_fields=("$",))
            raw = dict(raw)
        elif config_path is not None:
            raw = _read_config_document(config_path)
        else:
            raw = {}

        _reject_forbidden_fields(raw)
        _check_allowed_keys(raw, _ALLOWED_TOP_LEVEL, ())

        explicit_workspace = workspace if workspace is not None else self.workspace_override
        root = _resolve_workspace_root(
            raw,
            explicit_workspace=explicit_workspace,
            config_path=config_path,
        )
        if root.exists() and not root.is_dir():
            raise ConfigError(
                "configured Local_Workspace is not a directory",
                path="$.workspace",
                config_path=config_path,
                workspace=root,
                invalid_fields=("$.workspace",),
            )

        # Validate all raw path-bearing values before selecting a profile.  A
        # missing path is reported by source discovery later, but an escaping
        # path is rejected here and cannot be used accidentally.
        _validate_path_values(raw, root=root)

        selected_source_profile = (
            source_profile if source_profile is not None else self.source_profile_override
        )
        selected_fixture_profile = (
            fixture_profile if fixture_profile is not None else self.fixture_profile_override
        )
        selected_transport = source_transport if source_transport is not None else self.source_transport_override
        if selected_transport is not None and selected_transport not in {"auto", "http", "fixture"}:
            raise ConfigError(
                "source_transport must be auto, http, or fixture",
                path="$.source_transport",
                config_path=config_path,
                workspace=root,
                invalid_fields=("$.source_transport",),
            )

        return MonitorConfig(
            workspace=root,
            source=_parse_source(
                raw,
                workspace=root,
                config_path=config_path,
                source_profile_override=selected_source_profile,
                fixture_profile_override=selected_fixture_profile,
                transport_override=selected_transport,
            ),
            network=_parse_network(raw, config_path=config_path),
            validation=_parse_validation(raw, config_path=config_path),
            storage=_parse_storage(raw, workspace=root, config_path=config_path),
            presentation=_parse_presentation(raw, config_path=config_path),
            config_path=config_path,
        )

    def create_run_context(
        self,
        mode: RunMode = "sync",
        *,
        now: datetime | None = None,
        run_id: str | None = None,
        workspace: str | Path | None = None,
    ) -> RunContext:
        """Load configuration and create run metadata without filesystem writes."""

        return self.load(workspace=workspace).create_run_context(
            mode,
            now=now,
            run_id=run_id,
        )


def load_config(
    config_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
    source_profile: Any = None,
    fixture_profile: Any = None,
    source_transport: Transport | None = None,
    config: Mapping[str, Any] | None = None,
) -> MonitorConfig:
    """Functional convenience wrapper around :class:`ConfigLoader`."""

    return ConfigLoader(
        config_path,
        workspace=workspace,
        source_profile=source_profile,
        fixture_profile=fixture_profile,
        source_transport=source_transport,
        config=config,
    ).load()


load_configuration = load_config


__all__ = [
    "Config",
    "ConfigError",
    "ConfigLoader",
    "ConfigValidationError",
    "ConfigurationError",
    "DEFAULT_ANOMALY_THRESHOLD",
    "DEFAULT_DATE_FORMAT",
    "DEFAULT_LOCALE",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_PDF_MIN_BYTES",
    "DEFAULT_RETRY_DELAYS",
    "DEFAULT_SLUG_POLICY",
    "DEFAULT_STORAGE_DIRECTORIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "LocalMonitorConfig",
    "MonitorConfig",
    "NetworkConfig",
    "PresentationConfig",
    "RunContext",
    "SourceConfig",
    "Source_Profile",
    "SourceProfile",
    "Transport",
    "ValidationConfig",
    "load_config",
    "load_configuration",
]
