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


class DocGenerator:
    """Generate Markdown dashboard and pages from repository state."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self.data_dir = self.workspace_root / "data"
        self.standards_dir = self.data_dir / "standards"
        self.history_dir = self.workspace_root / "history"
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

    def _load_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
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
                if len(events) >= limit:
                    break
        except Exception:
            pass
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
            lines.extend([
                "| Дата | Тип события | Код | Стандарт | Измененные поля | Подробнее |",
                "|:---:|:---:|:---:|---|---|:---:|",
            ])
            for ev in events[:15]:
                dt = (ev.get("detected_at") or "")[:10]
                etype = ev.get("event_type", "")
                code = ev.get("code") or f"ID {ev.get('enbek_id')}"
                enbek_id = ev.get("enbek_id")
                link = f"[{code}](docs/standards/{enbek_id}.md)"
                title = ev.get("title") or ev.get("source", {}).get("title", "")
                changes = ev.get("changes") or {}
                change_desc = ", ".join(changes.keys()) if changes else etype
                lines.append(f"| {dt} | `{etype}` | **{link}** | {title} | `{change_desc}` | [Карточка →](docs/standards/{enbek_id}.md) |")
            lines.append("")
            lines.append("👉 *Полная хроника изменений доступна в файле [CHANGELOG.md](CHANGELOG.md).*")
            lines.append("")
        else:
            lines.extend([
                "> *Репозиторий находится в состоянии исходного базового снимка (baseline). При последующих еженедельных синхронизациях сюда будут автоматически добавляться строки с новыми и изменёнными стандартами.*",
                "",
            ])

        lines.extend([
            "## 📋 Каталог стандартов в базе",
            "",
            "| Код | Название стандарта | Секция ОКЭД | Госорган | Карточка | Документ |",
            "|:---:|---|---|---|:---:|:---:|",
        ])
        for r in records[:25]:
            enbek_id = r.get("enbek_id")
            code = r.get("code") or f"ID {enbek_id}"
            title = r.get("title") or "Без названия"
            oked = r.get("oked") or {}
            sec = oked.get("section_code") or "-"
            gov = r.get("government_body") or "-"
            pdf_path = self.workspace_root / "raw" / "pdf" / f"{enbek_id}.pdf"
            pdf_link = (
                f"[PDF 📄](raw/pdf/{enbek_id}.pdf)"
                if pdf_path.is_file()
                else "PDF отсутствует"
            )
            lines.append(
                f"| `{code}` | {title} | **{sec}** | {gov} | [Открыть](docs/standards/{enbek_id}.md) | {pdf_link} |"
            )
        if len(records) > 25:
            lines.append(f"\n*...и ещё {len(records) - 25} стандартов. Подробные карточки всех стандартов находятся в папке [`docs/standards/`](docs/standards/).*")
        lines.append("")

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
                    lines.append(f"| {dt} | `{ev.get('event_type')}` | {list((ev.get('changes') or {}).keys())} |")
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
