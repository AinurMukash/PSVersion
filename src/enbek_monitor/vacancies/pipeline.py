"""Weekly persistence boundary for hh vacancy snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..network import NetworkClient
from ..storage.raw import RawArtifactStore
from .analytics import summarize
from .collector import VacancyCollector
from .docs import PAGE_PATH, render_page, update_readme
from .history import diff_snapshots, load_previous, persist_snapshot
from .matching import match_records

HH_API_URL = "https://api.hh.ru/vacancies"
# hh area id for Kazakhstan (hh.kz). Keeps weekly snapshots KZ-focused
# instead of searching the whole hh.ru database when area is not given.
HH_KZ_AREA_ID = "40"


class _HttpxTransport:
    """Adapt httpx to the ``(url, method=..., params=...)`` transport contract.

    ``NetworkClient`` calls ``transport.request(url, method=..., ...)`` while
    ``httpx.Client.request`` takes ``(method, url)`` — passing the client
    directly raises ``multiple values for argument 'method'`` on live runs.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def request(self, url: str, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        response = self._client.request(
            kwargs.get("method", "GET"),
            url,
            params=kwargs.get("params"),
            headers=kwargs.get("headers"),
        )
        return SimpleNamespace(
            url=str(response.url),
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )


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
        max_pages: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = "hh-" + started.replace(":", "").replace("-", "")
        resolved_area = area if area else HH_KZ_AREA_ID
        resolved_limit = max(1, int(limit))
        resolved_pages = max_pages if max_pages else max(1, (resolved_limit + 99) // 100)
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
                _HttpxTransport(client),
                registry_url=HH_API_URL,
                allowed_hosts=("api.hh.ru",),
            )
            params: dict[str, Any] = {
                "text": query,
                "per_page": min(resolved_limit, 100),
                "area": resolved_area,
                "order_by": "publication_time",
            }
            result = VacancyCollector(network, self.raw_store).collect(
                [{"params": params}],
                run_id=run_id,
                max_pages=max(1, resolved_pages),
                limit=resolved_limit,
            )
        finally:
            client.close()

        previous = load_previous(self.data_dir / "current")
        changes = diff_snapshots(previous, result.records, run_id=run_id, detected_at=started)
        summary = summarize(result.records)
        matches = match_records(result.records, self.workspace_root / "data" / "standards")

        if result.success:
            status = "success"
        elif result.complete and result.records:
            status = "partial"
        else:
            status = "failed"

        report = {
            "schema_version": "1",
            "run_id": run_id,
            "started_at": started,
            "status": status,
            "query": query,
            "area": resolved_area,
            "limit": resolved_limit,
            "count": len(result.records),
            "new": changes["counts"]["new"],
            "updated": changes["counts"]["updated"],
            "removed": changes["counts"]["removed"],
            "unchanged": changes["counts"]["unchanged"],
            "changes": changes,
            "summary": summary,
            "matches": matches,
            "errors": [error.to_report_dict() for error in result.errors],
        }
        if not dry_run and result.complete:
            persist_snapshot(self.data_dir, result.records, changes)
            reports_dir = self.data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{run_id}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            events = list(changes.get("new", [])) + list(changes.get("updated", [])) + list(
                changes.get("removed", [])
            )
            page_path = self.workspace_root / PAGE_PATH
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                render_page(report, events, result.records), encoding="utf-8"
            )
            readme_path = self.workspace_root / "README.md"
            if readme_path.is_file():
                try:
                    readme_path.write_text(
                        update_readme(readme_path.read_text(encoding="utf-8"), report),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
        return report
