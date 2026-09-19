"""Shared pytest configuration for local, fixture-backed tests.

The manifest is loaded while pytest imports this file.  Consequently a broken
fixture index fails test collection/session setup before any parser or domain
test can consume a raw fixture path.
"""

from pathlib import Path

import pytest

from tests.fixtures.loader import (
    DEFAULT_MANIFEST_PATH,
    FixtureCase,
    FixtureManifest,
    SnapshotPairCase,
    load_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The manifest is optional for tests that do not need fixtures (e.g. hh
# vacancies). Load lazily so a missing manifest skips fixture-backed tests
# instead of breaking the whole pytest session on import.
try:
    FIXTURE_MANIFEST: FixtureManifest | None = load_manifest(DEFAULT_MANIFEST_PATH)
except Exception:
    FIXTURE_MANIFEST = None
FIXTURES_DIR: Path | None = FIXTURE_MANIFEST.fixtures_root if FIXTURE_MANIFEST else None


@pytest.fixture(scope="session")
def fixture_manifest() -> FixtureManifest:
    """Return the validated manifest used by every fixture-backed test."""

    if FIXTURE_MANIFEST is None:
        pytest.skip("fixture manifest is not available")
    return FIXTURE_MANIFEST


@pytest.fixture(scope="session")
def fixture_cases(fixture_manifest: FixtureManifest) -> tuple[FixtureCase, ...]:
    """Return typed ordinary cases; callers never need to scan the directory."""

    return fixture_manifest.all_cases


@pytest.fixture(scope="session")
def snapshot_pair_cases(
    fixture_manifest: FixtureManifest,
) -> tuple[SnapshotPairCase, ...]:
    """Return typed lifecycle snapshot-pair cases from the manifest."""

    return fixture_manifest.snapshot_pairs


@pytest.fixture(scope="session")
def fixtures_dir(fixture_manifest: FixtureManifest) -> Path:
    """Return the manifest-validated fixture directory."""

    return fixture_manifest.fixtures_root
