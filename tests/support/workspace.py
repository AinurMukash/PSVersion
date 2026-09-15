"""Isolated temporary workspaces and byte-preservation assertions.

The helpers deliberately use :class:`enbek_monitor.storage.workspace.WorkspaceGuard`
for path validation.  They never create files outside the supplied temporary
root and capture only durable monitor artifacts relevant to current/history/
docs preservation checks.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from enbek_monitor.storage.workspace import WorkspaceGuard


# ``current.json`` is a pointer in the designed layout.  The second path keeps
# the helper useful for early pipeline implementations that store the pointer
# directly under data/.
_CURRENT_POINTERS = (
    Path("data/snapshots/current.json"),
    Path("data/current.json"),
)
_ROOT_DOCUMENTS = (Path("README.md"), Path("CHANGELOG.md"))


@dataclass(frozen=True, slots=True)
class WorkspaceBytes:
    """Exact bytes of relevant existing artifacts at one observation point."""

    files: Mapping[str, bytes]

    def __getitem__(self, relative_path: str) -> bytes:
        return self.files[relative_path]

    def paths(self) -> tuple[str, ...]:
        return tuple(self.files)

    def as_dict(self) -> dict[str, bytes]:
        return dict(self.files)

    @property
    def empty(self) -> bool:
        return not bool(self.files)



def _iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise AssertionError(f"workspace artifact root is not a directory: {root}")
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda p: p.as_posix())



def capture_workspace_bytes(
    workspace: str | Path | "TemporaryWorkspace",
    *,
    include_current: bool = True,
    include_history: bool = True,
    include_docs: bool = True,
) -> WorkspaceBytes:
    """Capture exact bytes for current, history and Markdown artifacts.

    Missing files are represented by absence from the mapping, so a later
    creation or deletion is detected by an ordinary mapping comparison.
    ``work`` and raw staging are intentionally excluded: failed/dry-run tests
    normally allow diagnostic staging while asserting published state remains
    unchanged.
    """

    root = _workspace_root(workspace)
    paths: list[Path] = []
    if include_current:
        paths.extend(_CURRENT_POINTERS)
    if include_history:
        paths.extend(Path("history").joinpath(path.relative_to(root / "history")) for path in _iter_files(root / "history"))
    if include_docs:
        paths.extend(Path("docs").joinpath(path.relative_to(root / "docs")) for path in _iter_files(root / "docs"))
        paths.extend(_ROOT_DOCUMENTS)

    result: dict[str, bytes] = {}
    for relative in paths:
        path = root / relative
        if path.is_file():
            result[relative.as_posix()] = path.read_bytes()
        elif path.exists():
            raise AssertionError(f"workspace artifact path is not a file: {path}")
    return WorkspaceBytes(result)


# Short vocabulary aliases used by integration tests.
snapshot_workspace_bytes = capture_workspace_bytes
capture_artifact_bytes = capture_workspace_bytes



def assert_workspace_bytes_unchanged(
    before: WorkspaceBytes | Mapping[str, bytes],
    workspace: str | Path | "TemporaryWorkspace",
    *,
    message: str = "published workspace artifacts changed unexpectedly",
) -> WorkspaceBytes:
    """Assert that current/history/docs bytes match ``before`` exactly."""

    expected = before.files if isinstance(before, WorkspaceBytes) else dict(before)
    after = capture_workspace_bytes(workspace)
    actual = after.files
    if dict(expected) != dict(actual):
        missing = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        changed = sorted(
            path for path in set(expected).intersection(actual)
            if expected[path] != actual[path]
        )
        details = []
        if missing:
            details.append(f"missing={missing}")
        if added:
            details.append(f"added={added}")
        if changed:
            details.append(f"changed={changed}")
        raise AssertionError(message + (": " + "; ".join(details) if details else ""))
    return after


assert_artifacts_unchanged = assert_workspace_bytes_unchanged


class TemporaryWorkspace:
    """An isolated workspace root suitable for fixture-backed integration tests."""

    def __init__(self, path: str | Path | None = None, *, keep: bool = False) -> None:
        self._supplied_path = None if path is None else Path(path).expanduser()
        self.keep = keep
        self._temporary_directory: Path | None = None
        self._created_directory = False
        self.root: Path | None = None
        self.guard: WorkspaceGuard | None = None

    @property
    def path(self) -> Path:
        if self.root is None:
            raise RuntimeError("TemporaryWorkspace is not active")
        return self.root

    @property
    def workspace(self) -> Path:
        return self.path

    def __enter__(self) -> "TemporaryWorkspace":
        if self.root is not None:
            raise RuntimeError("TemporaryWorkspace cannot be entered twice")
        if self._supplied_path is None:
            root = Path(tempfile.mkdtemp(prefix="enbek-monitor-")).resolve()
            self._temporary_directory = root
            self._created_directory = True
        else:
            root = self._supplied_path.resolve()
            if root.exists() and not root.is_dir():
                raise NotADirectoryError(root)
            root.mkdir(parents=True, exist_ok=True)
            # A caller-owned path is never removed by this helper.  This keeps
            # the harness safe when it is given pytest's ``tmp_path`` or an
            # explicitly configured workspace.
            self._temporary_directory = None
            self._created_directory = False
        self.root = root
        self.guard = WorkspaceGuard(root)
        self.guard.preflight()
        # Creating only the allow-listed layout mirrors a real local run and
        # gives callers stable paths without writing config/cache elsewhere.
        for name in ("raw", "data", "history", "docs", "work"):
            self.guard.resolve_path(name, allow_directory=True).mkdir(
                parents=True,
                exist_ok=True,
            )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self.root is None:
            return
        root = self.root
        if self.guard is not None:
            self.guard.release_run()
        # The default workspace is owned by this helper and is removed unless
        # ``keep`` is requested.  Explicit paths are caller-owned and remain
        # intact regardless of ``keep``.
        if self._created_directory and not self.keep:
            shutil.rmtree(root, ignore_errors=True)
        self._temporary_directory = None
        self._created_directory = False
        self.root = None
        self.guard = None

    def capture(self, **kwargs: bool) -> WorkspaceBytes:
        return capture_workspace_bytes(self, **kwargs)

    def assert_unchanged(
        self,
        before: WorkspaceBytes | Mapping[str, bytes],
        **kwargs: str,
    ) -> WorkspaceBytes:
        return assert_workspace_bytes_unchanged(before, self, **kwargs)


@contextmanager
def temporary_workspace(
    path: str | Path | None = None,
    *,
    keep: bool = False,
) -> Iterator[TemporaryWorkspace]:
    """Yield a temporary, preflighted and isolated Local_Workspace."""

    with TemporaryWorkspace(path, keep=keep) as workspace:
        yield workspace


# Alias matching pytest fixture naming without requiring pytest at import time.
temporary_local_workspace = temporary_workspace


def _workspace_root(workspace: str | Path | TemporaryWorkspace) -> Path:
    if isinstance(workspace, TemporaryWorkspace):
        return workspace.path
    return Path(workspace).expanduser().resolve()


__all__ = [
    "TemporaryWorkspace",
    "WorkspaceBytes",
    "assert_artifacts_unchanged",
    "assert_workspace_bytes_unchanged",
    "capture_artifact_bytes",
    "capture_workspace_bytes",
    "snapshot_workspace_bytes",
    "temporary_local_workspace",
    "temporary_workspace",
]
