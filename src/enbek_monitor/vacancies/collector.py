"""API-first hh vacancy collector with fixture-friendly boundaries."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..errors import MonitorError, ParseError, SourceError
from ..models import ArtifactKind
from ..network import NetworkClient, SourceResponse
from ..storage.raw import RawArtifactStore
from . import parsers
from .models import VacancyRecord

HH_API_URL = "https://api.hh.ru/vacancies"
MAX_WORKERS = 5


@dataclass(frozen=True, slots=True)
class VacancyCollectionResult:
    records: tuple[VacancyRecord, ...] = ()
    raw_artifacts: tuple[Any, ...] = ()
    errors: tuple[MonitorError, ...] = ()
    complete: bool = False

    @property
    def success(self) -> bool:
        return self.complete and not self.errors

    @property
    def partial(self) -> bool:
        """Search traversal finished and at least one record was collected."""

        return self.complete and bool(self.records)


class VacancyCollector:
    """Collect one or more hh search profiles and their vacancy details."""

    def __init__(
        self,
        network_client: NetworkClient,
        raw_store: RawArtifactStore,
        *,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        self.network_client = network_client
        self.raw_store = raw_store
        self.max_workers = max(1, min(int(max_workers), MAX_WORKERS))

    def collect(
        self,
        profiles: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        run_id: str = "standalone",
        staging_root: str | None = None,
        fetched_at: str | None = None,
        max_pages: int = 1,
        limit: int | None = None,
    ) -> VacancyCollectionResult:
        records: dict[str, VacancyRecord] = {}
        artifacts: list[Any] = []
        errors: list[MonitorError] = []
        lock = threading.Lock()
        timestamp = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        search_ok = True

        for profile in profiles:
            url = str(profile.get("url", HH_API_URL))
            base_params = dict(profile.get("params", {}))
            per_page = _per_page(profile)
            if limit is not None and len(records) >= limit:
                break
            for page in range(max(1, max_pages)):
                if limit is not None and len(records) >= limit:
                    break
                params = dict(base_params)
                params["page"] = page
                params.setdefault("per_page", per_page)
                response = self.network_client.request(url, params=params, expected_kind="hh_search")
                if isinstance(response, SourceError):
                    errors.append(response)
                    search_ok = False
                    break
                if not isinstance(response, SourceResponse):
                    errors.append(SourceError("hh search returned no response", url=url, stage="hh collection"))
                    search_ok = False
                    break
                try:
                    with lock:
                        artifacts.append(self.raw_store.store(
                            ArtifactKind.HH_SEARCH, response.url, response.content,
                            content_type="application/json", http_status=response.status_code,
                            run_id=run_id, staging_root=staging_root,
                            headers=response.headers, fetched_at=timestamp,
                        ))
                    items, page_info = parsers.parse_search(response.json(), url=response.url)
                except (MonitorError, ValueError, TypeError, OSError) as exc:
                    errors.append(self._parse_error(url, str(exc)))
                    search_ok = False
                    break

                targets: list[tuple[str, str]] = []
                seen: set[str] = set(records)
                for item in items:
                    try:
                        vacancy_id, detail_url = parsers.search_identity(item, url=response.url)
                    except ParseError as exc:
                        errors.append(exc)
                        continue
                    if not vacancy_id or vacancy_id in seen:
                        continue
                    seen.add(vacancy_id)
                    if limit is not None and len(seen) > limit:
                        seen.discard(vacancy_id)
                        break
                    targets.append((vacancy_id, detail_url))

                self._fetch_details(targets, records, artifacts, errors, lock,
                                    run_id=run_id, staging_root=staging_root, timestamp=timestamp)

                if _last_page(items, page_info, page, per_page):
                    break

        return VacancyCollectionResult(
            tuple(records[vacancy_id] for vacancy_id in sorted(records)),
            tuple(artifacts),
            tuple(errors),
            search_ok,
        )

    def _fetch_details(
        self,
        targets: list[tuple[str, str]],
        records: dict[str, VacancyRecord],
        artifacts: list[Any],
        errors: list[MonitorError],
        lock: threading.Lock,
        *,
        run_id: str,
        staging_root: str | None,
        timestamp: str,
    ) -> None:
        if not targets:
            return

        def _one(target: tuple[str, str]) -> None:
            vacancy_id, detail_url = target
            detail = self.network_client.request(detail_url, expected_kind="hh_detail")
            if isinstance(detail, SourceError):
                with lock:
                    errors.append(detail)
                return
            if not isinstance(detail, SourceResponse):
                with lock:
                    errors.append(SourceError(
                        "hh detail returned no response", url=detail_url, stage="hh detail"))
                return
            try:
                artifact = self.raw_store.store(
                    ArtifactKind.HH_DETAIL, detail.url, detail.content,
                    vacancy_id=vacancy_id,
                    content_type="application/json", http_status=detail.status_code,
                    run_id=run_id, staging_root=staging_root,
                    headers=detail.headers, fetched_at=timestamp,
                )
                record = VacancyRecord(
                    **parsers.map_detail(detail.json()), fetched_at=timestamp)
            except (MonitorError, ValueError, TypeError, OSError, ParseError) as exc:
                with lock:
                    errors.append(self._parse_error(detail_url, str(exc)))
                return
            with lock:
                if vacancy_id not in records:
                    records[vacancy_id] = record
                    artifacts.append(artifact)

        if len(targets) == 1 or self.max_workers == 1:
            for target in targets:
                _one(target)
            return
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(targets))) as pool:
            list(pool.map(_one, targets))

    # Backwards-compatible helpers used by older tests/callers.
    @staticmethod
    def _items(response: SourceResponse) -> list[dict[str, Any]]:
        items, _ = parsers.parse_search(response.json(), url=response.url)
        return items

    @staticmethod
    def _record(payload: dict[str, Any], fetched_at: str) -> VacancyRecord:
        return VacancyRecord(**parsers.map_detail(payload), fetched_at=fetched_at)

    @staticmethod
    def _parse_error(url: str, reason: str) -> ParseError:
        return ParseError(reason, url=url, stage="hh collection", path="$", invalid_fields=("$",))


def _per_page(profile: dict[str, Any]) -> int:
    try:
        per_page = int((profile.get("params", {}) or {}).get("per_page", 20))
    except (TypeError, ValueError):
        return 20
    return per_page if per_page > 0 else 20


def _last_page(
    items: list[dict[str, Any]], page_info: dict[str, Any], page: int, per_page: int
) -> bool:
    if not items or len(items) < per_page:
        return True
    pages = page_info.get("pages")
    if isinstance(pages, int) and page + 1 >= pages:
        return True
    return False


# Backwards-compatible aliases for the pre-split helper names.
_nested_name = parsers.nested_name
_normalize_salary = parsers.normalize_salary
_requirements = parsers.professional_roles
_plain_text = parsers.plain_text
