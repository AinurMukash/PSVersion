"""Unit tests for catalog issue generation and CLI commands."""

from pathlib import Path
from unittest.mock import patch

from enbek_monitor.cli import main
from enbek_monitor.generate_docs import DocGenerator


def test_generate_catalog_parts_contains_all_records(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parent.parent
    generator = DocGenerator(workspace)

    records = generator._load_all_records()
    assert len(records) > 0, "Workspace should have existing standards records"

    parts = generator.generate_catalog_parts(max_chars=40000)
    assert len(parts) >= 1

    # Every part must be under 45,000 characters
    for idx, part in enumerate(parts):
        assert len(part) <= 45000, f"Part {idx+1} exceeds max_chars significantly"
        assert "| Код | Название стандарта |" in part

    # Part 1 must have the catalog marker and header
    assert "<!-- enbek-catalog-issue -->" in parts[0]
    assert "## 📋 Полный каталог профессиональных стандартов РК" in parts[0]

    # Verify that every standard enbek_id is present in one of the parts
    joined_text = "\n".join(parts)
    for record in records[:50]:  # sample check
        assert f"career.enbek.kz/ru/professionalstandart/{record['enbek_id']}" in joined_text


def test_generate_catalog_issue_returns_first_part(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parent.parent
    generator = DocGenerator(workspace)

    issue_body = generator.generate_catalog_issue(max_chars=40000)
    assert isinstance(issue_body, str)
    assert "<!-- enbek-catalog-issue -->" in issue_body
    assert len(issue_body) <= 45000


def test_readme_does_not_contain_catalog_table(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parent.parent
    generator = DocGenerator(workspace)

    readme_path = generator.generate_readme(
        records=generator._load_all_records(),
        events=generator._load_recent_events(),
    )
    content = readme_path.read_text(encoding="utf-8")

    assert "## 📋 Каталог стандартов в базе" not in content
    assert "GitHub Issues — Каталог стандартов" in content


def test_cli_catalog_issue_writes_parts(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parent.parent
    output_file = tmp_path / "test_catalog.md"

    with patch(
        "sys.argv",
        [
            "enbek-monitor",
            "catalog-issue",
            "--workspace",
            str(workspace),
            "--output",
            str(output_file),
        ],
    ):
        ret = main()
        assert ret == 0

    assert output_file.is_file()
    part1_text = output_file.read_text(encoding="utf-8")
    assert "<!-- enbek-catalog-issue -->" in part1_text

    # Multi-part output files check
    part_files = list(tmp_path.glob("test_catalog-part-*.md"))
    assert len(part_files) >= 1, "Should have created subsequent part files for comments"
