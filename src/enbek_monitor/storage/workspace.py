"""Safe local workspace boundaries and run locking.

This module owns filesystem preflight for a local monitor run.  It does not
create anything while validating a workspace: missing, non-directory,
non-writable, escaping or forbidden paths fail before an artifact can be
changed.  The canonical :class:`RunContext` remains the model defined by
``enbek_monitor.config``; it is re-exported here for storage callers.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..config import MonitorConfig, RunContext
from ..errors import WorkspaceError

ALLOWED_ARTIFACT_ROOTS = frozenset({"raw", "data", "history", "docs", "work"})
ALLOWED_ROOT_FILES = frozenset({"README.md", "CHANGELOG.md"})
LOCK_FILE_NAME = ".workspace.lock"
_METADATA_FILE_NAME = "run.json"


def _utc_second(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise WorkspaceError("run timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _has_agent_component(path: Path) -> bool:
    return any(part.casefold() == ".agent" for part in path.parts)


def _safe_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise WorkspaceError("run_id must be a non-empty path-safe string")
    value = run_id.strip()
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise WorkspaceError("run_id must be a non-empty path-safe string")
    if any(ord(char) < 32 for char in value):
        raise WorkspaceError("run_id must not contain control characters")
    return value


class WorkspaceLock:
    """Cross-process exclusive lock backed by atomic file creation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._token = secrets.token_hex(16)
        self._owned = False

    @property
    def acquired(self) -> bool:
        return self._owned

    def acquire(self) -> "WorkspaceLock":
        if self._owned:
            return self
        payload = {
            "pid": os.getpid(),
            "token": self._token,
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        try:
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                stat.S_IRUSR | stat.S_IWUSR,
            )
        except FileExistsError as exc:
            raise WorkspaceError(
                "Local_Workspace is already locked by another run",
                workspace_root=str(self.path.parent.parent),
                path=str(self.path),
                operation="acquire workspace lock",
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"could not acquire workspace lock: {exc}",
                workspace_root=str(self.path.parent.parent),
                path=str(self.path),
                operation="acquire workspace lock",
                permission=str(exc),
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._owned = True
        return self

    def release(self) -> None:
        if not self._owned:
            return
        try:
            # Never remove a lock that a different owner may have installed
            # after an external cleanup/recovery action.
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("token") == self._token:
                self.path.unlink(missing_ok=True)
        except (OSError, UnicodeError, json.JSONDecodeError):
            # The owner has no useful recovery action if the lock file vanished
            # or was corrupted; importantly, do not remove an unrelated file.
            pass
        finally:
            self._owned = False

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()


class WorkspaceGuard:
    """Validate and manage all filesystem access for one Local_Workspace."""

    def __init__(
        self,
        workspace: str | Path | MonitorConfig,
        *,
        config: MonitorConfig | None = None,
        allowed_roots: Mapping[str, str] | set[str] | frozenset[str] | None = None,
    ) -> None:
        if isinstance(workspace, MonitorConfig):
            if config is not None:
                raise WorkspaceError("workspace and config must not both be MonitorConfig")
            config = workspace
            workspace = config.workspace
        self.config = config
        self.root = Path(workspace).expanduser().resolve(strict=False)
        self.workspace = self.root
        configured = dict(config.storage.to_dict()) if config is not None else {
            "raw_dir": "raw",
            "data_dir": "data",
            "history_dir": "history",
            "docs_dir": "docs",
            "work_dir": "work",
        }
        if allowed_roots is not None:
            if isinstance(allowed_roots, Mapping):
                configured.update({str(key): str(value) for key, value in allowed_roots.items()})
            else:
                configured.update({f"root_{index}": str(value) for index, value in enumerate(allowed_roots)})
        self.storage_dirs = {
            name: Path(value)
            for name, value in configured.items()
            if name.endswith("_dir")
        }
        self._active_lock: WorkspaceLock | None = None
        self._active_context: RunContext | None = None

    @classmethod
    def from_config(cls, config: MonitorConfig) -> "WorkspaceGuard":
        return cls(config.workspace, config=config)

    def preflight(self) -> Path:
        """Check the root and all configured boundaries without writing files."""

        root = self.root
        if _has_agent_component(root):
            raise WorkspaceError(
                "Local_Workspace must not be .agent or contained by .agent",
                workspace_root=str(root),
                path=str(root),
                operation="workspace preflight",
            )
        if not root.exists():
            raise WorkspaceError(
                "Local_Workspace does not exist",
                workspace_root=str(root),
                path=str(root),
                operation="workspace preflight",
            )
        if not root.is_dir():
            raise WorkspaceError(
                "Local_Workspace is not a directory",
                workspace_root=str(root),
                path=str(root),
                operation="workspace preflight",
            )
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError(
                f"could not resolve Local_Workspace: {exc}",
                workspace_root=str(root),
                path=str(root),
                operation="workspace preflight",
            ) from exc
        if _has_agent_component(canonical_root):
            raise WorkspaceError(
                "Local_Workspace resolves into .agent",
                workspace_root=str(canonical_root),
                path=str(canonical_root),
                operation="workspace preflight",
            )
        if not _is_writable_directory(canonical_root):
            raise WorkspaceError(
                "Local_Workspace is not writable",
                workspace_root=str(canonical_root),
                path=str(canonical_root),
                operation="workspace preflight",
                permission="write",
            )
        self.root = canonical_root
        self.workspace = canonical_root
        for value in self.storage_dirs.values():
            directory = self.resolve_path(value, allow_directory=True)
            if directory.exists():
                if not directory.is_dir():
                    raise WorkspaceError(
                        "configured artifact root is not a directory",
                        workspace_root=str(self.root),
                        path=str(directory),
                        operation="workspace preflight",
                    )
                if not _is_writable_directory(directory):
                    raise WorkspaceError(
                        "configured artifact root is not writable",
                        workspace_root=str(self.root),
                        path=str(directory),
                        operation="workspace preflight",
                        permission="write",
                    )
        self.resolve_path("README.md")
        self.resolve_path("CHANGELOG.md")
        return canonical_root

    def resolve_path(self, path: str | Path, *, allow_directory: bool = False) -> Path:
        """Resolve an artifact path and enforce the workspace allow-list."""

        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        if _has_agent_component(resolved) or not _inside(self.root, resolved):
            raise WorkspaceError(
                "artifact path is outside Local_Workspace or enters .agent",
                workspace_root=str(self.root),
                path=str(path),
                operation="resolve artifact path",
            )
        relative = resolved.relative_to(self.root)
        if not relative.parts:
            if allow_directory:
                return resolved
            raise WorkspaceError(
                "the Local_Workspace root is not an artifact path",
                workspace_root=str(self.root),
                path=str(path),
                operation="resolve artifact path",
            )
        first = relative.parts[0]
        allowed_dirs = {value.parts[0] for value in self.storage_dirs.values() if value.parts}
        if first not in allowed_dirs and relative.as_posix() not in ALLOWED_ROOT_FILES:
            raise WorkspaceError(
                f"artifact path is not in the Local_Workspace allow-list: {path}",
                workspace_root=str(self.root),
                path=str(path),
                operation="resolve artifact path",
            )
        return resolved

    # Common vocabulary aliases used by storage callers.
    artifact_path = resolve_path
    path_for = resolve_path

    def _ensure_layout(self) -> None:
        for value in self.storage_dirs.values():
            directory = self.resolve_path(value, allow_directory=True)
            if directory.exists() and not directory.is_dir():
                raise WorkspaceError(
                    "configured artifact root is not a directory",
                    workspace_root=str(self.root),
                    path=str(directory),
                    operation="prepare workspace layout",
                )
        for value in self.storage_dirs.values():
            self.resolve_path(value, allow_directory=True).mkdir(parents=True, exist_ok=True)

    @property
    def work_root(self) -> Path:
        value = self.storage_dirs.get("work_dir", Path("work"))
        return self.resolve_path(value, allow_directory=True)

    @property
    def lock_path(self) -> Path:
        return self.resolve_path(self.work_root / LOCK_FILE_NAME)

    def _existing_run_ids(self) -> set[str]:
        result: set[str] = set()
        for name in ("history_dir", "work_dir"):
            directory = self.storage_dirs.get(name)
            if directory is None:
                continue
            candidate = self.resolve_path(directory, allow_directory=True)
            if not candidate.exists() or not candidate.is_dir():
                continue
            if name == "history_dir":
                runs = candidate / "runs"
                if runs.exists() and runs.is_dir():
                    result.update(item.stem for item in runs.iterdir() if item.is_file())
            else:
                runs = candidate / "runs"
                if runs.exists() and runs.is_dir():
                    result.update(item.name for item in runs.iterdir())
        return result

    def create_run_context(
        self,
        mode: str = "sync",
        *,
        registry_url: str | None = None,
        source_profile: Any = None,
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        """Build the canonical config ``RunContext`` after workspace checks."""

        self.preflight()
        if self.config is not None:
            if registry_url is not None and registry_url != self.config.registry_url:
                raise WorkspaceError("registry_url does not match the configured Source_Profile")
            candidate = self.config.create_run_context(mode, now=now, run_id=run_id)
        else:
            if source_profile is None:
                raise WorkspaceError(
                    "source_profile or MonitorConfig is required to create RunContext",
                    workspace_root=str(self.root),
                    operation="create run context",
                )
            exact_url = registry_url or getattr(source_profile, "registry_url", None)
            if not isinstance(exact_url, str) or not exact_url.strip():
                raise WorkspaceError("registry_url must be a non-empty string")
            if mode not in {"bootstrap", "sync", "dry_run"}:
                raise WorkspaceError(f"unsupported run mode: {mode!r}")
            started_at = _utc_second(now)
            chosen_id = _safe_run_id(run_id) if run_id is not None else ""
            staging_base = self.storage_dirs.get("work_dir", Path("work")) / "runs"
            if not chosen_id:
                while True:
                    chosen_id = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
                    if chosen_id not in self._existing_run_ids():
                        break
            candidate = RunContext(
                run_id=chosen_id,
                started_at=started_at,
                workspace=self.root,
                registry_url=exact_url,
                mode=mode,  # type: ignore[arg-type]
                staging_root=(self.root / staging_base / chosen_id).resolve(strict=False),
                source_profile=source_profile,
            )
        self._validate_context(candidate)
        if candidate.run_id in self._existing_run_ids():
            raise WorkspaceError(
                f"run_id is already used in this Local_Workspace: {candidate.run_id}",
                workspace_root=str(self.root),
                operation="create run context",
            )
        return candidate

    def _validate_context(self, context: RunContext) -> None:
        _safe_run_id(context.run_id)
        if context.workspace.resolve(strict=False) != self.root:
            raise WorkspaceError("RunContext workspace does not match Local_Workspace")
        self.resolve_path(context.staging_root, allow_directory=True)
        if not isinstance(context.registry_url, str) or not context.registry_url:
            raise WorkspaceError("RunContext registry_url must be exact and non-empty")
        if context.mode not in {"bootstrap", "sync", "dry_run"}:
            raise WorkspaceError(f"unsupported run mode: {context.mode!r}")

    def acquire_lock(self) -> WorkspaceLock:
        self.preflight()
        if self._active_lock is not None and self._active_lock.acquired:
            raise WorkspaceError("this WorkspaceGuard already owns an active run lock")
        self._ensure_layout()
        lock = WorkspaceLock(self.lock_path)
        lock.acquire()
        self._active_lock = lock
        return lock

    def release_lock(self) -> None:
        if self._active_lock is not None:
            self._active_lock.release()
            self._active_lock = None

    def start_run(
        self,
        mode: str = "sync",
        *,
        registry_url: str | None = None,
        source_profile: Any = None,
        now: datetime | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        """Acquire the workspace lock and create one run staging directory."""

        self.preflight()
        context = self.create_run_context(
            mode,
            registry_url=registry_url,
            source_profile=source_profile,
            now=now,
            run_id=run_id,
        )
        self.acquire_lock()
        try:
            staging = self.resolve_path(context.staging_root, allow_directory=True)
            if staging.exists() and not staging.is_dir():
                raise WorkspaceError("run staging path is not a directory")
            staging.mkdir(parents=True, exist_ok=False)
            metadata = self.resolve_path(staging / _METADATA_FILE_NAME)
            metadata.write_text(
                json.dumps(context.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self._active_context = context
            return context
        except BaseException:
            self.release_lock()
            raise

    def release_run(self, context: RunContext | None = None) -> None:
        if context is not None and self._active_context is not None:
            if context.run_id != self._active_context.run_id:
                raise WorkspaceError("run context does not match the active workspace lock")
        self._active_context = None
        self.release_lock()

    @contextmanager
    def locked_run(self, *args: Any, **kwargs: Any) -> Iterator[RunContext]:
        context = self.start_run(*args, **kwargs)
        try:
            yield context
        finally:
            self.release_run(context)

    run = locked_run


def _is_writable_directory(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
        if stat.S_IMODE(mode) & 0o222 == 0:
            return False
        return os.access(path, os.W_OK)
    except OSError:
        return False


__all__ = [
    "ALLOWED_ARTIFACT_ROOTS",
    "LOCK_FILE_NAME",
    "RunContext",
    "WorkspaceGuard",
    "WorkspaceLock",
]
