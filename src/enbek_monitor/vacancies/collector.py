"""API-first hh vacancy collector with fixture-friendly boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Any

from ..errors import MonitorError, ParseError, SourceError
from ..models import ArtifactKind
from ..network import NetworkClient, SourceResponse
from ..storage.raw import RawArtifactStore
from .models import VacancyRecord


@dataclass(frozen=True, slots=True)
class VacancyCollectionResult:
    records: tuple[VacancyRecord, ...] = ()
    raw_artifacts: tuple[Any, ...] = ()
    errors: tuple[MonitorError, ...] = ()
    complete: bool = False

    @property
    def success(self) -> bool:
        return self.complete and not self.errors


class VacancyCollector:
    """Collect one or more hh search profiles and their vacancy details."""

    def __init__(self, network_client: NetworkClient, raw_store: RawArtifactStore) -> None:
        self.network_client = network_client
        self.raw_store = raw_store

    def collect(
        self,
        profiles: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        run_id: str = "standalone",
        staging_root: str | None = None,
        fetched_at: str | None = None,
        max_pages: int = 1,
    ) -> VacancyCollectionResult:
        records: dict[str, VacancyRecord] = {}
        artifacts: list[Any] = []
        errors: list[MonitorError] = []
        timestamp = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for profile in profiles:
            url = str(profile.get("url", "https://api.hh.ru/vacancies"))
            params = dict(profile.get("params", {}))
            for page in range(max_pages):
                params["page"] = page
                response = self.network_client.request(url, params=params, expected_kind="hh_search")
                if isinstance(response, SourceError):
                    errors.append(response)
                    break
                if not isinstance(response, SourceResponse):
                    errors.append(SourceError("hh search returned no response", url=url, stage="hh collection"))
                    break
                try:
                    artifacts.append(self.raw_store.store(ArtifactKind.HH_SEARCH, response.url, response.content,
                        content_type="application/json", http_status=response.status_code,
                        run_id=run_id, staging_root=staging_root, headers=response.headers, fetched_at=timestamp))
                    items = self._items(response)
                except (MonitorError, ValueError, TypeError, OSError) as exc:
                    errors.append(self._parse_error(url, str(exc)))
                    break

                for item in items:
                    vacancy_id = str(item.get("id", "")).strip()
                    if not vacancy_id or vacancy_id in records:
                        continue
                    detail_url = str(item.get("url") or f"https://api.hh.ru/vacancies/{vacancy_id}")
                    detail = self.network_client.request(detail_url, expected_kind="hh_detail")
                    if isinstance(detail, SourceError):
                        errors.append(detail)
                        continue
                    if not isinstance(detail, SourceResponse):
                        errors.append(SourceError("hh detail returned no response", url=detail_url, stage="hh detail"))
                        continue
                    try:
                        artifacts.append(self.raw_store.store(ArtifactKind.HH_DETAIL, detail.url, detail.content,
                            content_type="application/json", http_status=detail.status_code,
                            run_id=run_id, staging_root=staging_root, headers=detail.headers, fetched_at=timestamp))
                        records[vacancy_id] = self._record(detail.json(), timestamp)
                    except (MonitorError, ValueError, TypeError, OSError, ParseError) as exc:
                        errors.append(self._parse_error(detail_url, str(exc)))

                if not items or len(items) < int(profile.get("params", {}).get("per_page", 20)):
                    break

        return VacancyCollectionResult(tuple(records.values()), tuple(artifacts), tuple(errors), not errors)

    @staticmethod
    def _items(response: SourceResponse) -> list[dict[str, Any]]:
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ParseError("hh search response must contain items", url=response.url, stage="hh search", path="$.items")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _record(payload: dict[str, Any], fetched_at: str) -> VacancyRecord:
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ParseError("hh vacancy detail must contain id", stage="hh detail", path="$.id")
        return VacancyRecord(
            vacancy_id=str(payload["id"]), title=payload.get("name"), url=payload.get("url"),
            alternate_url=payload.get("alternate_url"), description=_plain_text(payload.get("description")),
            employer=_nested_name(payload.get("employer")), area=_nested_name(payload.get("area")),
            published_at=payload.get("published_at"), updated_at=payload.get("updated_at"),
            experience=_nested_name(payload.get("experience")), employment=_nested_name(payload.get("employment")),
            schedule=_nested_name(payload.get("schedule")), salary=payload.get("salary") or {},
            key_skills=tuple(item.get("name", "") for item in payload.get("key_skills", []) if isinstance(item, dict)),
            fetched_at=fetched_at,
        )

    @staticmethod
    def _parse_error(url: str, reason: str) -> ParseError:
        return ParseError(reason, url=url, stage="hh collection", path="$", invalid_fields=("$",))


def _nested_name(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def _plain_text(value: Any) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip() or None