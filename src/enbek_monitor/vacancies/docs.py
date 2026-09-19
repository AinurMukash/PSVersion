"""Markdown rendering for hh vacancy snapshots.

Two artifacts, mirroring the standards docs boundary:

* ``docs/vacancies.md`` — full page: summary, tops, recent changes,
  standard matches, current vacancy table. Sections split by ``---``.
* README block between ``START``/``END`` markers — compact stats plus a
  link to the full page. Updated surgically (markers only), so both the
  enbek sync and the hh sync can refresh it without wiping each other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

START = "<!-- HH-VACANCIES:START -->"
END = "<!-- HH-VACANCIES:END -->"
PAGE_PATH = Path("docs/vacancies.md")


def render_section(report: Mapping[str, Any]) -> str:
    """Render the compact README block (without markers)."""
    query = report.get("query", "?")
    area = report.get("area", "?")
    started = str(report.get("started_at", ""))[:10]
    count = report.get("count", 0)
    new = report.get("new", 0)
    updated = report.get("updated", 0)
    removed = report.get("removed", 0)
    top_skills = ", ".join(
        item.get("name", "") for item in (report.get("summary") or {}).get("skills", [])[:5]
    ) or "—"
    return "\n".join([
        "## 💼 Рынок труда: вакансии hh.kz",
        "",
        f"> Еженедельный срез вакансий hh.kz (поиск: «{query}», регион: `{area}`, срез: `{started}`).",
        "",
        "| Показатель | Значение |",
        "|---|---:|",
        f"| Активных вакансий в срезе | **{count}** |",
        f"| Новых / обновлённых / ушедших | {new} / {updated} / {removed} |",
        f"| Топ навыков | {top_skills} |",
        "",
        "👉 *Подробности: [docs/vacancies.md](docs/vacancies.md) — топы, изменения, мэтчинг со стандартами.*",
        "",
    ])


def update_readme(text: str, report: Mapping[str, Any]) -> str:
    """Insert or refresh the vacancies block between markers."""
    block = f"{START}\n{render_section(report).rstrip()}\n{END}"
    if START in text and END in text:
        before, _, rest = text.partition(START)
        _, _, after = rest.partition(END)
        return f"{before}{block}{after}"
    anchor = "## 📂 Структура данных в репозитории"
    if anchor in text:
        return text.replace(anchor, f"{block}\n\n---\n\n{anchor}", 1)
    return text.rstrip() + f"\n\n---\n\n{block}\n"


def latest_report(data_dir: Path) -> dict[str, Any] | None:
    """Return the newest vacancy report, or None when no snapshot exists yet."""
    reports_dir = data_dir / "reports"
    if not reports_dir.exists():
        return None
    candidates = sorted(reports_dir.glob("*.json"))
    if not candidates:
        return None
    try:
        payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def changes_tail(data_dir: Path, limit: int = 15) -> list[dict[str, Any]]:
    """Return the most recent vacancy change events (newest first)."""
    log_path = data_dir / "changes.jsonl"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
        if len(events) >= limit:
            break
    return events


def render_page(
    report: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    records: Iterable[Any],
) -> str:
    """Render the full ``docs/vacancies.md`` page."""
    events = list(events)
    records = sorted(records, key=lambda r: _record_id(r))
    summary = report.get("summary") or {}
    query = report.get("query", "?")
    area = report.get("area", "?")
    started = str(report.get("started_at", ""))[:10]
    run_id = report.get("run_id", "")

    lines = [
        "# Вакансии hh.kz — срез рынка труда",
        "",
        f"**Поиск:** «{query}»  ",
        f"**Регион:** `{area}`  ",
        f"**Срез:** `{started}` (`{run_id}`)  ",
        "",
        "---",
        "",
        "## 📊 Сводка",
        "",
        "| Показатель | Значение |",
        "|---|---:|",
        f"| Активных вакансий | **{report.get('count', 0)}** |",
        f"| Новых | {report.get('new', 0)} |",
        f"| Обновлённых | {report.get('updated', 0)} |",
        f"| Ушедших из выдачи | {report.get('removed', 0)} |",
        f"| Без изменений | {report.get('unchanged', 0)} |",
        "",
        "---",
        "",
        "## 🔥 Топ навыков",
        "",
    ]
    lines.extend(_top_table(summary.get("skills", [])))
    lines.extend(["", "---", "", "## 🌍 Города", ""])
    lines.extend(_top_table(summary.get("areas", [])))
    lines.extend(["", "---", "", "## 🏢 Работодатели", ""])
    lines.extend(_top_table(summary.get("employers", [])))
    lines.extend(["", "---", "", "## 💰 Валюты зарплат", ""])
    lines.extend(_top_table(summary.get("currencies", [])))
    lines.extend(["", "---", "", "## 📢 Последние изменения", ""])
    if events:
        lines.extend([
            "| Дата | Тип | Вакансия | Изменённые поля |",
            "|:---:|:---:|---|---|",
        ])
        for event in events:
            dt = str(event.get("detected_at", ""))[:10]
            etype = event.get("event_type", "")
            title = _escape(event.get("title") or event.get("vacancy_id", ""))
            vid = event.get("vacancy_id", "")
            fields = ", ".join(event.get("changed_fields") or []) or "—"
            lines.append(f"| {dt} | `{etype}` | {title} (`{vid}`) | `{fields}` |")
    else:
        lines.append("> *Первый срез — baseline. Изменения появятся после следующих запусков.*")
    lines.extend(["", "---", "", "## 🎯 Соответствие профстандартам", ""])
    matches = report.get("matches") or []
    if matches:
        lines.extend([
            "| Вакансия | Стандарт | Уверенность | По признакам |",
            "|---|---|---:|---|",
        ])
        for match in matches[:30]:
            vid = match.get("vacancy_id", "")
            title = _escape(match.get("title") or "")
            sid = match.get("standard_id", "")
            conf = match.get("confidence", "")
            by = ", ".join(match.get("matched_by") or []) or "—"
            lines.append(f"| `{vid}` | {title} (`{sid}`) | {conf} | `{_escape(by)}` |")
        if len(matches) > 30:
            lines.append(f"\n*...и ещё {len(matches) - 30} совпадений.*")
    else:
        lines.append("> *Совпадений с профстандартами не найдено.*")
    lines.extend(["", "---", "", "## 📋 Текущие вакансии", ""])
    if records:
        lines.extend([
            "| Вакансия | Работодатель | Город | Зарплата | Ссылка |",
            "|---|---|---|---|:---:|",
        ])
        for record in records[:100]:
            title = _escape(_field(record, "title") or "")
            employer = _escape(_field(record, "employer") or "—")
            area_name = _escape(_field(record, "area") or "—")
            salary = _escape(_format_salary(_field(record, "salary")))
            link = _field(record, "alternate_url") or _field(record, "url") or ""
            link_md = f"[Открыть]({link})" if link else "—"
            lines.append(f"| {title} | {employer} | {area_name} | {salary} | {link_md} |")
        if len(records) > 100:
            lines.append(f"\n*...и ещё {len(records) - 100} вакансий. Машиночитаемые карточки — в [`data/vacancies/current/`](data/vacancies/current/).*")
    else:
        lines.append("> *Вакансий в срезе нет.*")
    lines.extend([
        "",
        "---",
        "",
        "| Директория / Файл | Назначение | Формат |",
        "|---|---|:---:|",
        "| [`data/vacancies/current/`](data/vacancies/current/) | Нормализованные карточки активного среза | `JSON` |",
        "| [`data/vacancies/reports/`](data/vacancies/reports/) | Отчёты о запусках со сводкой и мэтчингом | `JSON` |",
        "| [`data/vacancies/changes.jsonl`](data/vacancies/changes.jsonl) | Построчный лог `NEW`/`UPDATED`/`REMOVED` | `JSONL` |",
        "| [`raw/hh/`](raw/hh/) | Сырые ответы hh API | `JSON` |",
        "",
        "*Срез обновляется еженедельно по понедельникам с помощью GitHub Actions.*",
        "",
    ])
    return "\n".join(lines)


def _top_table(items: Any) -> list[str]:
    if not items:
        return ["> *Нет данных.*"]
    lines = ["| Название | Кол-во | Доля |", "|---|---:|---:|"]
    for item in list(items)[:15]:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"| {_escape(item.get('name', ''))} | {item.get('count', 0)} | {item.get('share', 0)} |"
        )
    return lines


def _record_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(record.get("vacancy_id", ""))
    return str(getattr(record, "vacancy_id", ""))


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _format_salary(salary: Any) -> str:
    if not isinstance(salary, Mapping) or not salary:
        return "—"
    currency = str(salary.get("currency") or "").upper()
    parts = []
    if salary.get("from") is not None:
        parts.append(f"от {_amount(salary['from'])}")
    if salary.get("to") is not None:
        parts.append(f"до {_amount(salary['to'])}")
    text = " ".join(parts) or "—"
    if currency:
        text += f" {currency}"
    if salary.get("gross") is True:
        text += " (до вычета)"
    return text


def _amount(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()
