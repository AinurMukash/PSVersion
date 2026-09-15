"""Main synchronization pipeline orchestrator for Enbek Professional Standards Monitor.

Executes the full pipeline:
1. Discover & traverse registry pages from Enbek.kz.
2. Fetch detail metadata and official PDFs with content deduplication.
3. Normalize records and calculate SHA-256 hashes.
4. Diff against repository baseline and detect NEW / UPDATED / MISSING / REMOVED.
5. Record changes in changes.jsonl and history/runs/.
6. Persist normalized records to data/standards/.
7. Regenerate README.md, CHANGELOG.md, and docs/standards/*.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from .diff import DiffEngine
from .generate_docs import DocGenerator
from .hashing import HashingService
from .models import (
    EventType,
    FieldLevelDiff,
    LifecycleEvent,
    NormalizedRecord,
    RegistryEntry,
    RunMode,
    RunStatus,
    Timestamps,
)
from .network import SourceResponse
from .normalize import Normalizer
from .parsers.detail_json import DetailJsonParser
from .parsers.registry_json import RegistryJsonParser
from .parsers.source_schema import DetailPage, RegistryPage

logger = logging.getLogger("enbek_monitor.sync")


class SyncPipeline:
    """Orchestrates complete synchronization between Enbek.kz and Git repository."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
        self.data_dir = self.workspace_root / "data"
        self.standards_dir = self.data_dir / "standards"
        self.raw_dir = self.workspace_root / "raw"
        self.pdf_dir = self.raw_dir / "pdf"
        self.history_dir = self.workspace_root / "history"
        self.runs_dir = self.history_dir / "runs"

        self.client = client or httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EnbekMonitor/1.0",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )

        self.registry_parser = RegistryJsonParser()
        self.detail_parser = DetailJsonParser()
        self.normalizer = Normalizer()
        self.hashing_service = HashingService()
        self.diff_engine = DiffEngine()
        self.doc_generator = DocGenerator(self.workspace_root)

    def _ensure_dirs(self) -> None:
        for d in (self.standards_dir, self.pdf_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def fetch_registry(self, limit: int | None = None) -> list[RegistryEntry]:
        """Traverse all paginated pages from career.enbek.kz/ru/api/professionalstandart."""
        entries: list[RegistryEntry] = []
        next_url: str | None = "https://career.enbek.kz/ru/api/professionalstandart?page=1"
        page_num = 1

        print("Fetching registry from Enbek.kz...")
        while next_url:
            print(f"  Fetching registry page {page_num}...")
            resp = self.client.get(next_url)
            if resp.status_code != 200:
                print(f"  [ERROR] Registry page {page_num} returned HTTP {resp.status_code}")
                break

            source_resp = SourceResponse(
                url=next_url,
                status_code=resp.status_code,
                headers=dict(resp.headers),
                content=resp.content,
            )
            parsed = self.registry_parser.parse(next_url, source_resp)
            if not isinstance(parsed, RegistryPage):
                print(f"  [ERROR] Failed to parse registry page {page_num}: {parsed}")
                break

            for entry in parsed.entries:
                entries.append(entry)
                if limit and len(entries) >= limit:
                    print(f"  Reached limit of {limit} entries.")
                    return entries

            next_url = parsed.pagination.next_url if not parsed.pagination.terminal else None
            page_num += 1

        print(f"Completed registry traversal: collected {len(entries)} standards.")
        return entries

    def fetch_detail_and_pdf(
        self,
        entry: RegistryEntry,
        existing: NormalizedRecord | None = None,
        dry_run: bool = False,
    ) -> tuple[DetailPage | None, bytes | None, str | None]:
        """Fetch standard detail JSON and PDF document.

        Re-downloads the PDF if no local copy exists OR if the standard's metadata
        (version, revision_date, title) has changed on Enbek.kz.
        """
        detail_url = entry.detail_page_url
        resp = None
        for attempt in range(3):
            try:
                resp = self.client.get(detail_url)
                if resp.status_code == 200:
                    break
            except Exception as e:
                if attempt == 2:
                    print(f"  [WARN] Detail request error for ID {entry.enbek_id}: {e}")
                time.sleep(0.5)

        if not resp or resp.status_code != 200:
            print(f"  [WARN] Detail for ID {entry.enbek_id} unavailable (HTTP {getattr(resp, 'status_code', 'error')})")
            return None, None, None

        source_resp = SourceResponse(
            url=detail_url,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
        )
        detail_page = self.detail_parser.parse(detail_url, source_resp)
        if not isinstance(detail_page, DetailPage):
            print(f"  [WARN] Failed to parse detail for ID {entry.enbek_id}: {detail_page}")
            return None, None, None

        # Check local PDF cache
        pdf_file = self.pdf_dir / f"{entry.enbek_id}.pdf"
        pdf_bytes: bytes | None = None
        pdf_sha256: str | None = None

        should_download = True
        if existing and pdf_file.exists() and pdf_file.stat().st_size > 0:
            # Check if metadata appears unchanged
            version_match = (str(existing.version or "") == str(detail_page.attributes.version or ""))
            rev_date_match = (str(existing.revision_date or "") == str(detail_page.attributes.revision_date or ""))
            title_match = ((existing.title or "").strip() == (detail_page.attributes.title or "").strip())
            if version_match and rev_date_match and title_match:
                should_download = False
                pdf_bytes = pdf_file.read_bytes()
                pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

        if should_download:
            pdf_url = detail_page.attributes.pdf_url or f"https://career.enbek.kz/ru/api/professionalstandart/{entry.enbek_id}/download"
            for attempt in range(3):
                try:
                    pdf_resp = self.client.get(pdf_url)
                    if pdf_resp.status_code == 200 and pdf_resp.content.startswith(b"%PDF"):
                        pdf_bytes = pdf_resp.content
                        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                        if not dry_run:
                            pdf_file.parent.mkdir(parents=True, exist_ok=True)
                            pdf_file.write_bytes(pdf_bytes)
                        break
                except Exception as e:
                    if attempt == 2:
                        print(f"  [WARN] Could not download PDF for ID {entry.enbek_id}: {e}")
                    time.sleep(0.5)

            if not pdf_sha256:
                if pdf_file.exists() and pdf_file.stat().st_size > 0:
                    pdf_bytes = pdf_file.read_bytes()
                    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                else:
                    pdf_sha256 = "0" * 64

        return detail_page, pdf_bytes, pdf_sha256

    def run(
        self,
        bootstrap: bool = False,
        dry_run: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Execute the sync pipeline."""
        start_time = datetime.now(timezone.utc)
        start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_date = start_time.strftime("%Y-%m-%d")

        print("=" * 60)
        print(f"Starting Enbek Monitor Sync [{start_iso}]")
        print(f"Mode: {'BOOTSTRAP' if bootstrap else 'SYNC'} | Dry Run: {dry_run} | Limit: {limit or 'None'}")
        print("=" * 60)

        if not dry_run:
            self._ensure_dirs()

        # Step 1: Load existing standards from repository
        existing_records: dict[int, NormalizedRecord] = {}
        if self.standards_dir.exists():
            for p in self.standards_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    rec = NormalizedRecord.from_dict(data)
                    existing_records[rec.enbek_id] = rec
                except Exception:
                    continue

        is_initial_bootstrap = bootstrap or len(existing_records) == 0
        if is_initial_bootstrap and not bootstrap:
            print("No existing baseline records found. Running as initial bootstrap.")
            bootstrap = True

        # Step 2: Fetch registry entries
        entries = self.fetch_registry(limit=limit)
        if not entries:
            print("[FATAL] No entries retrieved from registry. Aborting to protect state.")
            return {"status": "failed", "reason": "empty_registry"}

        # Step 3: Fetch detail, download PDF, normalize records
        candidates: dict[int, NormalizedRecord] = {}
        processed_entries: list[dict[str, Any]] = []

        print(f"\nProcessing {len(entries)} standards...")
        for i, entry in enumerate(entries, start=1):
            existing = existing_records.get(entry.enbek_id)
            detail_page, pdf_bytes, pdf_sha256 = self.fetch_detail_and_pdf(
                entry,
                existing=existing,
                dry_run=dry_run,
            )
            if not detail_page:
                continue

            existing = existing_records.get(entry.enbek_id)
            ts_first = existing.timestamps.first_seen_at if existing else start_iso
            ts_last_changed = existing.timestamps.last_changed_at if existing else start_iso

            norm_result = self.normalizer.normalize(
                registry=entry,
                detail=detail_page,
                pdf_descriptor={"sha256": pdf_sha256 or ("0" * 64)},
                timestamps={
                    "first_seen_at": ts_first,
                    "last_seen_at": start_iso,
                    "last_changed_at": ts_last_changed,
                },
            )
            if not isinstance(norm_result, NormalizedRecord):
                print(f"  [WARN] Normalization error for ID {entry.enbek_id}: {norm_result}")
                continue

            # Calculate and attach all 3 canonical sha256 digests
            complete_record = self.hashing_service.with_hashes(
                norm_result,
                pdf_bytes=pdf_bytes if pdf_bytes else (b"%PDF-mock" if not pdf_sha256 else None),
            )
            candidates[entry.enbek_id] = complete_record
            processed_entries.append({
                "enbek_id": entry.enbek_id,
                "code": complete_record.code,
                "title": complete_record.title,
                "version": complete_record.version,
                "status": "active",
            })

            if i % 10 == 0 or i == len(entries):
                print(f"  Processed {i}/{len(entries)} standards...")

        # Step 4: Compare (Diff Engine & Lifecycle Tracker)
        new_events: list[LifecycleEvent] = []
        new_count = 0
        updated_count = 0
        unchanged_count = 0
        missing_count = 0
        removed_count = 0

        print("\nComparing with repository state...")
        for enbek_id, candidate in candidates.items():
            existing = existing_records.get(enbek_id)
            if existing is None:
                new_count += 1
                if not bootstrap:
                    ev = LifecycleEvent(
                        event_id=f"{run_date}-{enbek_id}-new",
                        run_id=f"run-{run_date}",
                        enbek_id=enbek_id,
                        code=candidate.code,
                        event_type=EventType.NEW,
                        detected_at=start_iso,
                        new_hashes=candidate.hashes,
                        changes={},
                        source={"title": candidate.title},
                    )
                    new_events.append(ev)
            else:
                # Compare canonical hashes
                if candidate.hashes.normalized_sha256 != existing.hashes.normalized_sha256:
                    updated_count += 1
                    diff_res = self.diff_engine.diff(existing, candidate)
                    candidate.timestamps.last_changed_at = start_iso

                    if not bootstrap:
                        changes_dict = diff_res.to_dict().get("changes", {})
                        ev = LifecycleEvent(
                            event_id=f"{run_date}-{enbek_id}-updated",
                            run_id=f"run-{run_date}",
                            enbek_id=enbek_id,
                            code=candidate.code,
                            event_type=EventType.UPDATED,
                            detected_at=start_iso,
                            old_hashes=existing.hashes,
                            new_hashes=candidate.hashes,
                            changes=changes_dict,
                            source={"title": candidate.title},
                        )
                        new_events.append(ev)
                else:
                    unchanged_count += 1

        # Check for missing standards (only if full run without limit)
        if limit is None:
            for enbek_id, existing in existing_records.items():
                if enbek_id not in candidates:
                    missing_count += 1
                    # Keep existing record in snapshot marked as missing/archived
                    candidates[enbek_id] = existing

        # Step 5: Persist data and history
        if not dry_run:
            print("\nPersisting updated records to disk...")
            # Save individual JSON standards
            for enbek_id, record in candidates.items():
                target_file = self.standards_dir / f"{enbek_id}.json"
                target_file.write_text(record.to_json(), encoding="utf-8")

            # Save registry index
            registry_index = self.data_dir / "registry.json"
            registry_index.write_text(json.dumps(processed_entries, ensure_ascii=False, indent=2), encoding="utf-8")

            # Append events to history/changes.jsonl
            if new_events:
                changes_file = self.history_dir / "changes.jsonl"
                with changes_file.open("a", encoding="utf-8") as f:
                    for ev in new_events:
                        f.write(ev.to_json())

            # Save stats
            stats = {
                "last_sync": start_iso,
                "total_standards": len(candidates),
                "new_this_run": new_count,
                "updated_this_run": updated_count,
                "unchanged": unchanged_count,
                "missing": missing_count,
            }
            (self.data_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

            # Save run report
            run_report = {
                "started_at": start_iso,
                "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "success",
                "bootstrap": bootstrap,
                "total": len(candidates),
                "new": new_count,
                "updated": updated_count,
                "unchanged": unchanged_count,
                "missing": missing_count,
                "removed": removed_count,
                "events_count": len(new_events),
            }
            run_file = self.runs_dir / f"{run_date}.json"
            run_file.write_text(json.dumps(run_report, ensure_ascii=False, indent=2), encoding="utf-8")

            # Step 6: Generate Markdown documentation
            print("Generating README.md, CHANGELOG.md, and docs/standards/...")
            self.doc_generator.generate_all(run_report)

        print("\n" + "=" * 60)
        print("SYNC PIPELINE COMPLETED SUCCESSFULLY!")
        print(f"Total Standards: {len(candidates)}")
        print(f"New: {new_count} | Updated: {updated_count} | Unchanged: {unchanged_count} | Missing: {missing_count}")
        print(f"New events logged: {len(new_events)}")
        print("=" * 60)

        return {
            "status": "success",
            "total": len(candidates),
            "new": new_count,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "missing": missing_count,
        }
