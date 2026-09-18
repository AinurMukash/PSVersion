"""Weekly persistence boundary for hh vacancy snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..network import NetworkClient
from ..storage.raw import RawArtifactStore
from .collector import VacancyCollector
from .analytics import summarize
from .matching import match_records


class VacancyPipeline:
    """Collect and publish one weekly normalized vacancy snapshot."""

    def __init__(self, workspace_root: Path | str | None = None) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.data_dir = self.workspace_root / "data" / "vacancies"
        self.raw_store = RawArtifactStore(self.workspace_root)

    def run(
        self,
        *,
        query: str,
        area: str | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "hh-" + started.replace(":", "").replace("-", "")
        client = httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": "enbek-professional-standards-monitor/0.1 (market research)",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        try:
            network = NetworkClient(
                client,
                registry_url="https://api.hh.ru/vacancies",
                allowed_hosts=("api.hh.ru",),
            )
            params: dict[str, Any] = {"text": query, "per_page": min(max(limit, 1), 100)}
            if area:
                params["area"] = area
            result = VacancyCollector(network, self.raw_store).collect(
                [{"params": params}], run_id=run_id, max_pages=max(1, (limit + 99) // 100)
            )
        finally:
            client.close()

        report = {
            "schema_version": "1",
            "run_id": run_id,
            "started_at": started,
            "status": "success" if result.success else "failed",
            "query": query,
            "area": area,
            "count": len(result.records),
            "summary": summarize(result.records),
            "matches": match_records(result.records, self.workspace_root / "data" / "standards"),
            "errors": [error.to_report_dict() for error in result.errors],
        }
        if not dry_run and result.success:
            current_dir = self.data_dir / "current"
            current_dir.mkdir(parents=True, exist_ok=True)
            for record in result.records:
                (current_dir / f"{record.vacancy_id}.json").write_text(
                    json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            reports_dir = self.data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{run_id}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return report