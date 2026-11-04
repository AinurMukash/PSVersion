"""Unit tests for issue reporting and documentation generation."""

import json
from pathlib import Path
from unittest.mock import patch

from enbek_monitor.cli import main
from enbek_monitor.generate_docs import DocGenerator


def test_readme_does_not_contain_catalog_table_or_issue_link() -> None:
    workspace = Path(__file__).resolve().parent.parent
    generator = DocGenerator(workspace)

    readme_path = generator.generate_readme(
        records=generator._load_all_records(),
        events=generator._load_recent_events(),
    )
    content = readme_path.read_text(encoding="utf-8")

    # Verify that neither the old truncated 25-item table nor the catalog issue link exists
    assert "## 📋 Каталог стандартов в базе" not in content
    assert "GitHub Issues — Каталог стандартов" not in content
    assert "docs/standards/" in content
    assert "docs/oked/" in content
    assert "data/registry.json" in content


def test_issue_summary_no_changes_when_latest_run_has_zero_events() -> None:
    workspace = Path(__file__).resolve().parent.parent
    generator = DocGenerator(workspace)

    summary = generator.generate_issue_summary()
    # The latest run in repo (2026-11-04) had 0 events
    assert "Изменения не зафиксированы" in summary
    assert "<!-- enbek-run-id:" not in summary


def test_issue_summary_formats_events_when_changes_occur(tmp_path: Path) -> None:
    # Setup a mock workspace with changes
    history_dir = tmp_path / "history"
    history_dir.mkdir(parents=True)
    runs_dir = history_dir / "runs"
    runs_dir.mkdir()
    data_dir = tmp_path / "data" / "standards"
    data_dir.mkdir(parents=True)

    # 1. Write run report with 1 event
    run_report = {
        "started_at": "2026-11-10T08:00:00Z",
        "finished_at": "2026-11-10T08:03:00Z",
        "events_count": 1,
        "new": 1,
        "updated": 0,
        "removed": 0,
    }
    (runs_dir / "2026-11-10.json").write_text(json.dumps(run_report), encoding="utf-8")

    # 2. Write event to changes.jsonl
    event = {
        "event_id": "run-2026-11-10:9999:NEW",
        "run_id": "run-2026-11-10",
        "enbek_id": 9999,
        "code": "TEST01",
        "event_type": "NEW",
        "detected_at": "2026-11-10T08:01:00Z",
        "changes": [],
    }
    (history_dir / "changes.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    # 3. Write standard card
    card = {
        "enbek_id": 9999,
        "code": "TEST01",
        "title": "Тестовый стандарт",
        "version": 1,
        "year": 2026,
    }
    (data_dir / "9999.json").write_text(json.dumps(card), encoding="utf-8")

    generator = DocGenerator(tmp_path)
    summary = generator.generate_issue_summary()

    assert "<!-- enbek-run-id:run-2026-11-10 -->" in summary
    assert "Резюме обновления профессиональных стандартов" in summary
    assert "TEST01" in summary
    assert "Тестовый стандарт" in summary
    assert "`NEW`" in summary


def test_cli_issue_summary_writes_output_file(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parent.parent
    output_file = tmp_path / "issue_summary.md"

    with patch(
        "sys.argv",
        [
            "enbek-monitor",
            "issue-summary",
            "--workspace",
            str(workspace),
            "--output",
            str(output_file),
        ],
    ):
        ret = main()
        assert ret == 0

    assert output_file.is_file()
    content = output_file.read_text(encoding="utf-8")
    assert "## Обновление профессиональных стандартов" in content
