"""Automated Markdown dashboard and documentation generator.

Builds human-readable views directly from normalized records and history:
- README.md: Main dashboard with registry status, metrics, OKED, and ministries.
- CHANGELOG.md: Human-readable changelog from history/changes.jsonl.
- docs/standards/{id}.md: Per-standard documentation pages.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import NormalizedRecord


def _format_change_fields(changes: Any, fallback: str = "") -> list[str]:
    if not changes:
        return [fallback] if fallback else []
    if isinstance(changes, dict):
        return list(changes.keys())
    if isinstance(changes, (list, tuple)):
        fields = []
        for item in changes:
            if isinstance(item, dict) and "path" in item:
                fields.append(str(item["path"]).replace("$.", ""))
            elif hasattr(item, "path"):
                fields.append(str(item.path).replace("$.", ""))
        return fields or ([fallback] if fallback else [])
    return [fallback] if fallback else []


class DocGenerator:
    """Generate Markdown dashboard and pages from repository state."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self.data_dir = self.workspace_root / "data"
        self.standards_dir = self.data_dir / "standards"
        self.history_dir = self.workspace_root / "history"
        self.runs_dir = self.history_dir / "runs"
        self.docs_dir = self.workspace_root / "docs"
        self.standards_docs_dir = self.docs_dir / "standards"

    def generate_all(self, run_report: Mapping[str, Any] | None = None) -> None:
        """Generate README, CHANGELOG, and individual standard pages."""
        records = self._load_all_records()
        events = self._load_recent_events()
        
        self.generate_readme(records, events, run_report)
        self.generate_changelog()
        self.generate_standard_pages(records, events)
        self.generate_oked_pages(records)

    def _load_all_records(self) -> list[dict[str, Any]]:
        records = []
        if not self.standards_dir.exists():
            return records
        for p in sorted(self.standards_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                records.append(data)
            except Exception:
                continue
        return records

    def _load_recent_events(self, limit: int | None = 50) -> list[dict[str, Any]]:
        changes_file = self.history_dir / "changes.jsonl"
        events = []
        if not changes_file.exists():
            return events
        try:
            lines = changes_file.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line:
                    events.append(json.loads(line))
                if limit is not None and len(events) >= limit:
                    break
        except Exception:
            pass
        return events

    def _load_latest_run_events(self) -> list[dict[str, Any]]:
        """Read only the newest contiguous run from the append-only journal."""
        # If the latest run in history/runs had 0 events, there are no new changes
        if self.runs_dir.exists():
            run_files = sorted(self.runs_dir.glob("*.json"))
            if run_files:
                latest_run_file = run_files[-1]
                try:
                    report = json.loads(latest_run_file.read_text(encoding="utf-8"))
                    if isinstance(report, dict):
                        if report.get("events_count", 0) == 0 and not (
                            report.get("new", 0) or report.get("updated", 0) or report.get("removed", 0)
                        ):
                            return []
                except (OSError, json.JSONDecodeError):
                    pass

        changes_file = self.history_dir / "changes.jsonl"
        if not changes_file.exists():
            return []

        events: list[dict[str, Any]] = []
        run_id: str | None = None
        chunk_size = 8192

        try:
            with changes_file.open("rb") as journal:
                position = journal.seek(0, 2)
                remainder = b""

                while position > 0:
                    size = min(chunk_size, position)
                    position -= size
                    journal.seek(position)
                    lines = (journal.read(size) + remainder).split(b"\n")
                    remainder = lines[0]

                    for raw_line in reversed(lines[1:]):
                        if not raw_line.strip():
                            continue
                        try:
                            event = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue

                        event_run_id = event.get("run_id")
                        if not event_run_id:
                            continue
                        if run_id is None:
                            run_id = event_run_id
                        elif event_run_id != run_id:
                            return events
                        events.append(event)

                if remainder.strip():
                    try:
                        event = json.loads(remainder)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        event = None
                    if isinstance(event, dict):
                        event_run_id = event.get("run_id")
                        if event_run_id and (run_id is None or event_run_id == run_id):
                            events.append(event)
        except OSError:
            return []

        return events

    def generate_readme(
        self,
        records: list[dict[str, Any]],
        events: list[dict[str, Any]],
        run_report: Mapping[str, Any] | None = None,
    ) -> Path:
        readme_path = self.workspace_root / "README.md"
        total = len(records)
        
        now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

        # Aggregate by OKED
        oked_counts: Counter[str] = Counter()
        oked_names: dict[str, str] = {}
        ministry_counts: Counter[str] = Counter()

        for r in records:
            oked = r.get("oked") or {}
            sec_code = oked.get("section_code") or "Без секции"
            sec_name = oked.get("section_name") or ""
            oked_counts[sec_code] += 1
            if sec_name and sec_code not in oked_names:
                oked_names[sec_code] = sec_name

            gov = r.get("government_body") or "Не указан"
            ministry_counts[gov] += 1

        metrics: Mapping[str, Any] = run_report or {}
        if not metrics:
            stats_path = self.data_dir / "stats.json"
            try:
                loaded_stats = json.loads(stats_path.read_text(encoding="utf-8"))
                if isinstance(loaded_stats, dict):
                    metrics = loaded_stats
            except (OSError, UnicodeError, json.JSONDecodeError):
                metrics = {}

        new_count = metrics.get("new", metrics.get("new_this_run", 0))
        updated_count = metrics.get("updated", metrics.get("updated_this_run", 0))
        removed_count = metrics.get("removed", 0)
        missing_pdf_count = sum(
            not (self.workspace_root / "raw" / "pdf" / f"{record.get('enbek_id')}.pdf").is_file()
            for record in records
            if record.get("enbek_id")
        )
        if "missing_pdf" in metrics:
            missing_pdf_count = metrics["missing_pdf"]

        lines = [
            "# Профессиональные стандарты Республики Казахстан",
            "",
            "> Автоматизированный Git-мониторинг реестра профессиональных стандартов с официального портала [career.enbek.kz](https://career.enbek.kz/ru/ps-registry).",
            "",
            f"**Последняя синхронизация:** `{now_str}`",
            f"**Всего стандартов в репозитории:** `{total}`",
            "**Периодичность обновления:** еженедельно по понедельникам (GitHub Actions)",
            "",
            "## 📊 Состояние реестра",
            "",
            "| Показатель | Значение |",
            "|---|---:|",
            f"| Всего стандартов в базе | **{total}** |",
            f"| Новых в последнем запуске | {new_count} |",
            f"| Обновлено в последнем запуске | {updated_count} |",
            f"| Удалено / в архиве | {removed_count} |",
            f"| Стандартов без локального PDF | {missing_pdf_count} |",
            "",
            "## 📢 Журнал изменений (Последние обновления)",
            "",
        ]

        if events:
            records_by_id = {
                record.get("enbek_id"): record
                for record in records
                if record.get("enbek_id") is not None
            }
            lines.extend([
                "| Дата | Событие | Код | Название стандарта | Кратко об изменении | Карточка |",
                "|:---:|:---:|:---:|---|---|:---:|",
            ])
            for ev in events[:15]:
                dt = (ev.get("detected_at") or "")[:10]
                etype = ev.get("event_type", "")
                enbek_id = ev.get("enbek_id")
                code = _escape_table_cell(ev.get("code") or f"ID {enbek_id}")
                record = records_by_id.get(enbek_id, {})
                title = _escape_table_cell(
                    record.get("title") or _event_title_from_changes(ev.get("changes")) or "Без названия"
                )
                change_desc = _escape_table_cell(_summarize_event(ev, record))
                lines.append(
                    f"| {dt} | `{_escape_table_cell(etype)}` | `{code}` | {title} | "
                    f"{change_desc} | [Карточка →](docs/standards/{enbek_id}.md) |"
                )
            lines.append("")
            lines.append("👉 *Полная хроника изменений доступна в файле [CHANGELOG.md](CHANGELOG.md).*")
            lines.append("")
        else:
            lines.extend([
                "> *Репозиторий находится в состоянии исходного базового снимка (baseline). При последующих еженедельных синхронизациях сюда будут автоматически добавляться строки с новыми и изменёнными стандартами.*",
                "",
            ])

        lines.extend([
            "📋 *Карточки всех стандартов доступны в папке [`docs/standards/`](docs/standards/), а также по отраслям в [`docs/oked/`](docs/oked/). Полный реестр в JSON: [`data/registry.json`](data/registry.json).*",
            "",
        ])

        lines.extend([
            "## 🏭 Распределение по отраслям (ОКЭД)",
            "",
            "| Секция | Отрасль | Стандартов |",
            "|:---:|---|---:|",
        ])
        for sec_code in sorted(oked_counts.keys()):
            name = oked_names.get(sec_code, "-")
            lines.append(
                f"| [**{sec_code}**](docs/oked/{sec_code}.md) | {name} | {oked_counts[sec_code]} |"
            )
        lines.append("")

        lines.extend([
            "## 🏛️ Распределение по государственным органам",
            "",
            "| Государственный орган | Количество стандартов |",
            "|---|---:|",
        ])
        for gov, count in ministry_counts.most_common(20):
            lines.append(f"| {gov} | {count} |")
        lines.append("")

        lines.extend(self._vacancies_section())

        lines.extend([
            "## 📂 Структура данных в репозитории",
            "",
            "| Директория / Файл | Назначение | Формат |",
            "|---|---|:---:|",
            "| [`docs/standards/`](docs/standards/) | Полные человекочитаемые карточки стандартов с профессиями и ОРК | `Markdown` |",
            "| [`docs/oked/`](docs/oked/) | Каталоги стандартов с навигацией по секциям ОКЭД | `Markdown` |",
            "| [`docs/vacancies.md`](docs/vacancies.md) | Срез вакансий hh.kz: сводка, топы, изменения и мэтчинг со стандартами | `Markdown` |",
            "| [`CHANGELOG.md`](CHANGELOG.md) | Хронологический журнал всех зарегистрированных изменений | `Markdown` |",
            "| [`data/standards/`](data/standards/) | Нормализованные машиночитаемые карточки каждого стандарта | `JSON` |",
            "| [`data/vacancies/`](data/vacancies/) | Снапшоты вакансий hh.kz (`current/`), отчёты (`reports/`) и лог изменений (`changes.jsonl`) | `JSON`+`JSONL` |",
            "| [`data/registry.json`](data/registry.json) | Компактный реестр всех стандартов для поиска и аналитики | `JSON` |",
            "| [`data/stats.json`](data/stats.json) | Сводная агрегированная статистика по отраслям и статусам | `JSON` |",
            "| [`raw/pdf/`](raw/pdf/) | Оригинальные официальные PDF-документы профессиональных стандартов | `PDF` |",
            "| [`raw/hh/`](raw/hh/) | Сырые ответы hh API (поиск и детали вакансий) | `JSON` |",
            "| [`history/changes.jsonl`](history/changes.jsonl) | Построчный аудит-лог событий (`NEW`, `UPDATED`, `REMOVED`) с field-level diff | `JSONL` |",
            "| [`history/runs/`](history/runs/) | Технические отчёты о каждом еженедельном запуске краулера | `JSON` |",
            "",
            "---",
            "*Данные автоматически синхронизируются еженедельно с помощью GitHub Actions.*",
            "",
        ])

        readme_path.write_text("\n".join(lines), encoding="utf-8")
        return readme_path

    def _vacancies_section(self) -> list[str]:
        """Render the hh vacancies README block (with markers).

        The block carries the same START/END markers the vacancy pipeline
        uses for surgical updates, so either weekly job refreshes the same
        place and neither wipes the other. Without a snapshot yet, a
        baseline placeholder is rendered instead of nothing.
        """
        try:
            from .vacancies import docs as vacancy_docs
        except Exception:
            return []
        try:
            report = vacancy_docs.latest_report(self.data_dir / "vacancies")
        except Exception:
            return []
        return [
            vacancy_docs.START,
            vacancy_docs.render_section(report).rstrip(),
            vacancy_docs.END,
            "",
            "---",
            "",
        ]

    def generate_oked_pages(self, records: list[dict[str, Any]]) -> None:
        """Generate one navigable standard catalog for every OKED section."""
        oked_dir = self.docs_dir / "oked"
        oked_dir.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        names: dict[str, str] = {}

        for record in records:
            oked = record.get("oked") or {}
            section_code = oked.get("section_code") or "Без-секции"
            grouped[section_code].append(record)
            names.setdefault(section_code, oked.get("section_name") or "Без названия")

        for section_code, section_records in sorted(grouped.items()):
            section_name = names[section_code]
            lines = [
                f"# ОКЭД {section_code} — {section_name}",
                "",
                f"**Стандартов в разделе:** {len(section_records)}  ",
                "",
                "| Код | Стандарт | Госорган | Карточка | PDF |",
                "|---|---|---|:---:|:---:|",
            ]
            for record in sorted(
                section_records,
                key=lambda item: (item.get("title") or "", item.get("enbek_id") or 0),
            ):
                enbek_id = record.get("enbek_id")
                code = record.get("code") or f"ID {enbek_id}"
                title = record.get("title") or "Без названия"
                government_body = record.get("government_body") or "-"
                pdf_path = self.workspace_root / "raw" / "pdf" / f"{enbek_id}.pdf"
                pdf_link = (
                    f"[PDF](../../raw/pdf/{enbek_id}.pdf)"
                    if pdf_path.is_file()
                    else "PDF отсутствует"
                )
                lines.append(
                    f"| `{code}` | {title} | {government_body} | "
                    f"[Открыть](../standards/{enbek_id}.md) | "
                    f"{pdf_link} |"
                )
            lines.extend(["", "[Вернуться к сводке](../../README.md)", ""])
            (oked_dir / f"{section_code}.md").write_text("\n".join(lines), encoding="utf-8")

    def generate_changelog(self) -> Path:
        changelog_path = self.workspace_root / "CHANGELOG.md"
        events = self._load_recent_events(limit=500)
        
        events_by_date = defaultdict(list)
        for ev in events:
            dt = (ev.get("detected_at") or "UNKNOWN")[:10]
            events_by_date[dt].append(ev)

        lines = [
            "# Журнал изменений профессиональных стандартов",
            "",
            "> Все зафиксированные изменения в реестре профессиональных стандартов Enbek.kz.",
            "",
        ]

        if not events_by_date:
            lines.append("*История изменений пуста или находится на этапе первичного среза (baseline).*")
        else:
            for dt in sorted(events_by_date.keys(), reverse=True):
                day_events = events_by_date[dt]
                lines.append(f"## {dt}")
                lines.append("")
                for ev in day_events:
                    etype = ev.get("event_type")
                    code = ev.get("code") or f"ID {ev.get('enbek_id')}"
                    enbek_id = ev.get("enbek_id")
                    title = ev.get("title") or ""
                    link = f"[{code}](docs/standards/{enbek_id}.md)"
                    lines.append(f"- **`{etype}`** {link} — {title}")
                    changes = ev.get("changes")
                    if isinstance(changes, dict) and changes:
                        for field_name, delta in changes.items():
                            if isinstance(delta, dict) and "old" in delta and "new" in delta:
                                lines.append(f"  - `{field_name}`: `{delta['old']}` → `{delta['new']}`")
                            else:
                                lines.append(f"  - `{field_name}` изменено")
                    elif isinstance(changes, (list, tuple)) and changes:
                        for diff_item in changes:
                            if isinstance(diff_item, dict):
                                f_name = str(diff_item.get("path", "")).replace("$.", "")
                                old_v = diff_item.get("old")
                                new_v = diff_item.get("new")
                                if old_v is not None or new_v is not None:
                                    lines.append(f"  - `{f_name}`: `{old_v}` → `{new_v}`")
                                else:
                                    lines.append(f"  - `{f_name}` изменено")
                lines.append("")

        changelog_path.write_text("\n".join(lines), encoding="utf-8")
        return changelog_path

    def generate_standard_pages(
        self,
        records: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> None:
        self.standards_docs_dir.mkdir(parents=True, exist_ok=True)
        events_by_id = defaultdict(list)
        for ev in events:
            events_by_id[ev.get("enbek_id")].append(ev)

        for r in records:
            enbek_id = r.get("enbek_id")
            if not enbek_id:
                continue
            code = r.get("code") or "БЕЗ_КОДА"
            title = r.get("title") or "Без названия"
            doc_file = self.standards_docs_dir / f"{enbek_id}.md"

            oked = r.get("oked") or {}
            professions = r.get("professions") or []
            source = r.get("source") or {}
            hashes = r.get("hashes") or {}
            timestamps = r.get("timestamps") or {}

            lines = [
                f"# {code} — {title}",
                "",
                f"**Enbek ID:** `{enbek_id}`  ",
                f"**Версия:** `{r.get('version') or '-'}`  ",
                f"**Год утверждения:** `{r.get('year') or '-'}`  ",
                f"**Дата пересмотра:** `{r.get('revision_date') or '-'}`  ",
                f"**Государственный орган:** {r.get('government_body') or '-'}  ",
                "",
                "## Описание / Область применения",
                "",
                r.get("description") or "*Описание не предоставлено.*",
                "",
                "## ОКЭД",
                "",
                f"- **Секция:** `{oked.get('section_code') or '-'}` — {oked.get('section_name') or '-'}",
                f"- **Подкласс:** `{oked.get('subclass_code') or '-'}` — {oked.get('subclass_name') or '-'}",
                "",
                "## Профессии и уровни ОРК",
                "",
            ]

            if professions:
                lines.extend([
                    "| Профессия | Уровень ОРК |",
                    "|---|:---:|",
                ])
                for p in professions:
                    lines.append(f"| {p.get('name') or '-'} | {p.get('ork_level') or '-'} |")
            else:
                lines.append("*Список профессий не указан.*")
            lines.append("")

            # History table
            st_events = events_by_id.get(enbek_id, [])
            lines.extend([
                "## История изменений",
                "",
            ])
            if st_events:
                lines.extend([
                    "| Дата | Событие | Детали |",
                    "|---|:---:|---|",
                ])
                for ev in st_events:
                    dt = (ev.get("detected_at") or "")[:10]
                    change_fields = _format_change_fields(ev.get("changes"), ev.get("event_type", ""))
                    lines.append(f"| {dt} | `{ev.get('event_type')}` | {', '.join(change_fields)} |")
            else:
                lines.append(f"- Зафиксирован в базовом снимке: `{timestamps.get('first_seen_at') or '-'}`")
            lines.append("")

            lines.extend([
                "## Документы и ссылки",
                "",
                f"- [Карточка на Enbek.kz]({source.get('page_url') or source.get('registry_url') or '#'})",
                f"- [Скачать официальный PDF]({source.get('pdf_url') or '#'})",
                f"- [Локальная копия PDF (в репозитории)](../../raw/pdf/{enbek_id}.pdf)",
                "",
                "## Контрольные суммы (SHA-256)",
                "",
                f"- **Метаданные:** `{hashes.get('metadata_sha256') or '-'}`",
                f"- **PDF документ:** `{hashes.get('pdf_sha256') or '-'}`",
                f"- **Нормализованная запись:** `{hashes.get('normalized_sha256') or '-'}`",
                "",
            ])

            doc_file.write_text("\n".join(lines), encoding="utf-8")

    def generate_issue_summary(self, max_chars: int = 60000) -> str:
        """Generate a concise Markdown summary for GitHub Issue notifications."""
        events = self._load_latest_run_events()
        if not events:
            return "## Обновление профессиональных стандартов\n\nИзменения не зафиксированы."

        # Keep one notification tied to one synchronization run. This stable
        # marker lets the workflow retry safely after an issue API failure.
        latest_run_id = events[0].get("run_id")
        if not latest_run_id:
            latest_date = (events[0].get("detected_at") or "")[:10]
            recent = [event for event in events if (event.get("detected_at") or "")[:10] == latest_date]
            latest_run_id = f"run-{latest_date}" if latest_date else "run-unknown"
        else:
            recent = events

        latest_date = (recent[0].get("detected_at") or "")[:10] or latest_run_id.removeprefix("run-")
        max_chars = max(256, max_chars)

        lines = [
            f"## Резюме обновления профессиональных стандартов ({latest_date})",
            "",
            f"Всего зафиксировано событий в данном обновлении: **{len(recent)}**.",
            "",
            f"<!-- enbek-run-id:{latest_run_id} -->",
            "",
            "| ID | Код | Профессиональный стандарт | Событие | Кратко об изменении | Карточка |",
            "|---|---|---|---|---|---|",
        ]

        omitted = 0
        for index, ev in enumerate(recent):
            enbek_id = ev.get("enbek_id")
            code = _escape_table_cell(ev.get("code") or f"ID {enbek_id}")
            event_type = str(ev.get("event_type") or "UNKNOWN")
            etype = _escape_table_cell(event_type)
            record = self._load_standard_record(enbek_id)
            title = _escape_table_cell(
                record.get("title") or _event_title_from_changes(ev.get("changes")) or "Без названия"
            )
            change_summary = _escape_table_cell(_summarize_event(ev, record))
            card_url = _standard_url(enbek_id)
            row = (
                f"| {enbek_id} | `{code}` | {title} | `{etype}` | "
                f"{change_summary} | [Enbek]({card_url}) |"
            )
            if len("\n".join(lines + [row])) > max_chars - 100:
                omitted = len(recent) - index
                break
            lines.append(row)

        if omitted:
            lines.append(f"| ... | ... | ... | *(еще {omitted} событий опущено)* | |")

        lines.extend([
            "",
            "Подробности доступны в [журнале изменений](https://github.com/"
            "AinurMukash/enbek-profstandars/blob/main/CHANGELOG.md).",
            "",
        ])

        return "\n".join(lines)

    def generate_catalog_parts(self, max_chars: int = 40000) -> list[str]:
        """Generate a full catalog of all standards split into parts for GitHub Issue + comments."""
        records = self._load_all_records()
        total = len(records)
        now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

        parts_rows: list[list[str]] = []
        current_part_rows: list[str] = []
        current_len = 0

        header_template = (
            "| Код | Название стандарта | Секция ОКЭД | Госорган | Карточка |\n"
            "|:---:|---|:---:|---|:---:|\n"
        )
        base_header_len = len(header_template) + 500

        for r in records:
            enbek_id = r.get("enbek_id")
            code = _escape_table_cell(r.get("code") or f"ID {enbek_id}")
            title = _escape_table_cell(r.get("title") or "Без названия")
            oked = r.get("oked") or {}
            sec = _escape_table_cell(oked.get("section_code") or "-")
            gov = _escape_table_cell(r.get("government_body") or "-")
            card_url = _standard_url(enbek_id)
            row = f"| `{code}` | {title} | **{sec}** | {gov} | [Открыть]({card_url}) |"
            row_len = len(row) + 1

            if current_part_rows and (current_len + row_len > max(256, max_chars)):
                parts_rows.append(current_part_rows)
                current_part_rows = [row]
                current_len = base_header_len + row_len
            else:
                if not current_part_rows:
                    current_len = base_header_len + row_len
                else:
                    current_len += row_len
                current_part_rows.append(row)

        if current_part_rows:
            parts_rows.append(current_part_rows)

        total_parts = len(parts_rows) or 1
        results: list[str] = []

        for idx, rows in enumerate(parts_rows, start=1):
            lines = []
            if idx == 1:
                lines.extend([
                    "## 📋 Полный каталог профессиональных стандартов РК",
                    "",
                    f"Всего стандартов в реестре: **{total}** — актуально на `{now_str}`.",
                ])
                if total_parts > 1:
                    lines.extend([
                        "",
                        f"> ℹ️ *Каталог разбит на {total_parts} части (Часть 1 здесь, остальные {total_parts - 1} части — в комментариях ниже).* ",
                    ])
                lines.extend([
                    "",
                    "<!-- enbek-catalog-issue -->",
                    "",
                    "| Код | Название стандарта | Секция ОКЭД | Госорган | Карточка |",
                    "|:---:|---|:---:|---|:---:|",
                ])
            else:
                lines.extend([
                    f"### 📋 Полный каталог профессиональных стандартов РК (Часть {idx} из {total_parts})",
                    "",
                    "| Код | Название стандарта | Секция ОКЭД | Госорган | Карточка |",
                    "|:---:|---|:---:|---|:---:|",
                ])

            lines.extend(rows)

            if idx == total_parts:
                lines.extend([
                    "",
                    "---",
                    "Подробные карточки стандартов в папке `docs/standards/`. "
                    f"Официальный реестр на [career.enbek.kz](https://career.enbek.kz/ru/ps-registry).",
                    "",
                ])

            results.append("\n".join(lines))

        return results

    def generate_catalog_issue(self, max_chars: int = 40000) -> str:
        """Generate the first part (or full single-part) of the catalog issue."""
        parts = self.generate_catalog_parts(max_chars=max_chars)
        return parts[0] if parts else ""

    def _load_standard_record(self, enbek_id: Any) -> dict[str, Any]:
        """Load one normalized standard record for issue-summary context."""
        try:
            standard_id = int(enbek_id)
        except (TypeError, ValueError):
            return {}
        if standard_id <= 0:
            return {}
        path = self.standards_dir / f"{standard_id}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return record if isinstance(record, dict) else {}


def _escape_table_cell(value: Any) -> str:
    """Keep registry-provided text from breaking the Markdown table."""
    return (
        str(value)
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("\r", " ")
        .replace("\n", "<br>")
    )


def _standard_url(enbek_id: Any) -> str:
    """Build the public Enbek card URL for a registry identifier."""
    try:
        return f"https://career.enbek.kz/ru/professionalstandart/{int(enbek_id)}"
    except (TypeError, ValueError):
        return "https://career.enbek.kz/ru/ps-registry"


def _event_title_from_changes(changes: Any) -> str | None:
    """Recover a standard title from the title field diff when needed."""
    if not isinstance(changes, (list, tuple)):
        return None
    for change in changes:
        if isinstance(change, Mapping):
            path = change.get("path")
            value = change.get("new") or change.get("old")
        else:
            path = getattr(change, "path", None)
            value = getattr(change, "new", None) or getattr(change, "old", None)
        if path in {"$.title", "title"} and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _summarize_event(event: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    """Replace noisy JSON paths with a short, reader-friendly change summary."""
    event_type = str(event.get("event_type") or "UNKNOWN")
    if event_type == "NEW":
        details = []
        if record.get("version") is not None:
            details.append(f"версия {record['version']}")
        if record.get("year") is not None:
            details.append(f"год {record['year']}")
        professions = record.get("professions")
        if isinstance(professions, list) and professions:
            details.append(f"профессий: {len(professions)}")
        return "Добавлен новый стандарт" + (f" ({', '.join(details)})" if details else "")
    if event_type == "REMOVED":
        return "Стандарт удалён из реестра"
    if event_type == "RESTORED":
        return "Стандарт вернулся в реестр"

    labels = {
        "title": "название",
        "government_body": "госорган",
        "version": "версия",
        "year": "год",
        "revision_date": "дата пересмотра",
        "description": "описание",
        "oked": "ОКЭД",
        "professions": "профессии и уровни ОРК",
    }
    changes = event.get("changes") or []
    if isinstance(changes, Mapping):
        changes = [{"path": path, "old": None, "new": value} for path, value in changes.items()]

    summaries: list[str] = []
    seen_labels: set[str] = set()
    if isinstance(changes, (list, tuple)):
        for change in changes:
            if isinstance(change, Mapping):
                path = str(change.get("path") or "")
                old, new = change.get("old"), change.get("new")
            else:
                path = str(getattr(change, "path", ""))
                old, new = getattr(change, "old", None), getattr(change, "new", None)
            normalized_path = path.removeprefix("$").lstrip(".")
            root = normalized_path.split(".", 1)[0].split("[", 1)[0]
            label = labels.get(root)
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            if root in {"title", "professions", "oked", "description"}:
                summaries.append(label)
            else:
                summaries.append(f"{label}: {_short_value(old)} → {_short_value(new)}")

    return "; ".join(summaries) if summaries else "Изменений значимых полей нет"


def _short_value(value: Any, max_chars: int = 70) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text
