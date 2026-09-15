"""Focused tests for append-only raw artifact persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from enbek_monitor.errors import SourceError, WorkspaceError
from enbek_monitor.network import SourceResponse
from enbek_monitor.storage.raw import RawArtifactStore
from enbek_monitor.storage.workspace import WorkspaceGuard


FETCHED_AT = datetime(2025, 1, 2, 3, 4, 5, 900000, tzinfo=timezone.utc)
PDF_BYTES = b"%PDF-1.7\n" + b"x" * (1024 - len(b"%PDF-1.7\n"))


def response(
    url: str,
    body: bytes,
    *,
    content_type: str,
    status: int = 200,
) -> SourceResponse:
    return SourceResponse(
        url=url,
        status_code=status,
        headers={"Content-Type": content_type},
        content=body,
    )


def test_store_registry_detail_and_pdf_preserves_exact_bytes_and_manifest(
    tmp_path: Path,
) -> None:
    store = RawArtifactStore(tmp_path)
    staging = tmp_path / "work" / "runs" / "run-1"

    registry_body = b'{"items":[{"id":1}]}'
    detail_body = b'{"enbek_id":1,"title":"Standard"}'
    registry = store.store_registry(
        response(
            "https://career.enbek.kz/registry?page=1",
            registry_body,
            content_type="application/json",
        ),
        run_id="run-1",
        staging_root=staging,
        fetched_at=FETCHED_AT,
    )
    detail = store.store_detail(
        response(
            "https://career.enbek.kz/standard/1",
            detail_body,
            content_type="application/json",
        ),
        enbek_id=1,
        run_id="run-1",
        staging_root=staging,
        fetched_at=FETCHED_AT,
    )
    pdf = store.store_pdf(
        response(
            "https://career.enbek.kz/standard/1.pdf",
            PDF_BYTES,
            content_type="application/pdf; charset=binary",
        ),
        enbek_id=1,
        fetched_at=FETCHED_AT,
    )

    assert registry.physical_path.startswith("work/runs/run-1/raw/registry/")
    assert detail.physical_path.startswith("work/runs/run-1/raw/detail/")
    assert pdf.physical_path.startswith("raw/pdf/1/2025-01-02T03-04-05Z_")
    assert pdf.logical_key == "enbek-1"
    assert (tmp_path / registry.physical_path).read_bytes() == registry_body
    assert (tmp_path / detail.physical_path).read_bytes() == detail_body
    assert (tmp_path / pdf.physical_path).read_bytes() == PDF_BYTES
    assert registry.fetched_at == "2025-01-02T03:04:05Z"
    assert pdf.byte_length == len(PDF_BYTES)
    assert len(pdf.sha256 or "") == 64

    lines = store.manifest_path.read_bytes().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["source_url"] for line in lines] == [
        "https://career.enbek.kz/registry?page=1",
        "https://career.enbek.kz/standard/1",
        "https://career.enbek.kz/standard/1.pdf",
    ]


def test_manifest_is_append_only_and_existing_artifact_is_not_overwritten(
    tmp_path: Path,
) -> None:
    store = RawArtifactStore(tmp_path)
    first = store.store_registry(
        response("https://example.test/registry", b"first", content_type="text/html"),
        run_id="same-run",
        fetched_at=FETCHED_AT,
    )
    before = store.manifest_path.read_bytes()
    second = store.store_registry(
        response("https://example.test/registry?page=2", b"second", content_type="text/html"),
        run_id="same-run",
        fetched_at=FETCHED_AT,
    )

    assert (tmp_path / first.physical_path).read_bytes() == b"first"
    assert (tmp_path / second.physical_path).read_bytes() == b"second"
    after = store.manifest_path.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 2

    # A content-addressed PDF may be observed twice, but its bytes are still
    # immutable and the manifest receives a new observation line.
    pdf = response("https://example.test/1.pdf", PDF_BYTES, content_type="application/pdf")
    first_pdf = store.store_pdf(pdf, enbek_id=1, fetched_at=FETCHED_AT)
    second_pdf = store.store_pdf(pdf, enbek_id=1, fetched_at=FETCHED_AT)
    assert first_pdf.physical_path == second_pdf.physical_path
    assert (tmp_path / first_pdf.physical_path).read_bytes() == PDF_BYTES
    assert len(store.manifest_path.read_bytes().splitlines()) == 4


def test_invalid_pdf_response_is_rejected_without_publishing_artifacts(
    tmp_path: Path,
) -> None:
    store = RawArtifactStore(tmp_path)
    invalid_cases = (
        response("https://example.test/404.pdf", PDF_BYTES, content_type="application/pdf", status=404),
        response("https://example.test/short.pdf", b"short", content_type="application/pdf"),
        response("https://example.test/html.pdf", PDF_BYTES, content_type="text/html"),
    )

    for item in invalid_cases:
        with pytest.raises(SourceError):
            store.store_pdf(item, enbek_id=7, fetched_at=FETCHED_AT)

    assert not (tmp_path / "raw" / "pdf").exists()
    assert not store.manifest_path.exists()


def test_all_store_paths_are_guarded_and_manifest_descriptor_is_verified(
    tmp_path: Path,
) -> None:
    guard = WorkspaceGuard(tmp_path)
    store = RawArtifactStore(guard)

    with pytest.raises(ValueError, match="path-safe"):
        store.store_registry(
            response("https://example.test/registry", b"data", content_type="application/json"),
            run_id="../outside",
        )
    with pytest.raises(WorkspaceError):
        store.store_registry(
            response("https://example.test/registry", b"data", content_type="application/json"),
            staging_root=tmp_path.parent / "outside",
        )

    artifact = store.store_registry(
        response("https://example.test/registry", b"data", content_type="application/json"),
        run_id="safe",
        fetched_at=FETCHED_AT,
    )
    path = tmp_path / artifact.physical_path
    path.write_bytes(b"tampered")
    with pytest.raises(WorkspaceError, match="SHA-256"):
        store.append_manifest(artifact)
    assert len(store.manifest_path.read_bytes().splitlines()) == 1
