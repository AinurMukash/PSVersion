"""Command Line Interface for Enbek Professional Standards Monitor.

Usage:
  python -m enbek_monitor.cli sync [--bootstrap] [--dry-run] [--limit N]
  python -m enbek_monitor.cli docs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .generate_docs import DocGenerator
from .sync import SyncPipeline
from .vacancies.pipeline import VacancyPipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="enbek-monitor",
        description="Automated Enbek.kz professional standards monitor",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: sync
    sync_parser = subparsers.add_parser("sync", help="Synchronize standards with Enbek.kz")
    sync_parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Initial baseline run (does not record NEW/UPDATED diff events)",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and diff without writing any files to disk",
    )
    sync_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of standards to process (for testing/quick check)",
    )
    sync_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace root path (defaults to current directory)",
    )
    sync_parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent workers for detail fetching (default: 8)",
    )

    # Command: docs
    docs_parser = subparsers.add_parser("docs", help="Regenerate README and Markdown documentation")
    docs_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace root path (defaults to current directory)",
    )

    vacancies_parser = subparsers.add_parser(
        "vacancies-sync", help="Synchronize weekly hh vacancy data"
    )
    vacancies_parser.add_argument("--query", required=True, help="hh vacancy search query")
    vacancies_parser.add_argument("--area", default=None, help="hh area ID (default 40 = Kazakhstan)")
    vacancies_parser.add_argument("--limit", type=int, default=100)
    vacancies_parser.add_argument("--max-pages", type=int, default=None)
    vacancies_parser.add_argument("--dry-run", action="store_true")
    vacancies_parser.add_argument("--workspace", type=str, default=None)
    vacancies_parser.add_argument("--token", type=str, default=None, help="HeadHunter API OAuth access token")

    # Command: issue-summary
    issue_parser = subparsers.add_parser(
        "issue-summary", help="Generate Markdown summary for GitHub Issue notification"
    )
    issue_parser.add_argument("--workspace", type=str, default=None, help="Workspace root path")
    issue_parser.add_argument("--output", type=str, default=None, help="Output file path")

    # Command: catalog-issue
    catalog_parser = subparsers.add_parser(
        "catalog-issue", help="Generate full catalog of all standards for GitHub Issue"
    )
    catalog_parser.add_argument("--workspace", type=str, default=None, help="Workspace root path")
    catalog_parser.add_argument("--output", type=str, default=None, help="Output file path")

    args = parser.parse_args()

    if not args.command or args.command == "sync":
        bootstrap = getattr(args, "bootstrap", False)
        dry_run = getattr(args, "dry_run", False)
        limit = getattr(args, "limit", None)
        workspace = getattr(args, "workspace", None)
        workers = getattr(args, "workers", 8)

        pipeline = SyncPipeline(workspace_root=workspace, max_workers=workers)
        result = pipeline.run(bootstrap=bootstrap, dry_run=dry_run, limit=limit)
        return 0 if result.get("status") == "success" else 1

    elif args.command == "docs":
        workspace = getattr(args, "workspace", None)
        generator = DocGenerator(workspace or Path.cwd())
        generator.generate_all()
        print("Documentation regenerated successfully.")
        return 0

    elif args.command == "vacancies-sync":
        report = VacancyPipeline(args.workspace).run(
            query=args.query,
            area=args.area,
            limit=args.limit,
            max_pages=getattr(args, "max_pages", None),
            dry_run=args.dry_run,
            token=getattr(args, "token", None),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in ("success", "partial") else 1

    elif args.command == "issue-summary":
        workspace = getattr(args, "workspace", None)
        generator = DocGenerator(workspace or Path.cwd())
        summary = generator.generate_issue_summary()
        output_path = getattr(args, "output", None)
        if output_path:
            Path(output_path).write_text(summary, encoding="utf-8")
        else:
            print(summary)
        return 0

    elif args.command == "catalog-issue":
        workspace = getattr(args, "workspace", None)
        generator = DocGenerator(workspace or Path.cwd())
        parts = generator.generate_catalog_parts()
        output_path = getattr(args, "output", None)
        if output_path:
            out_p = Path(output_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(parts[0], encoding="utf-8")
            for idx, part_content in enumerate(parts[1:], start=2):
                part_file = out_p.parent / f"{out_p.stem}-part-{idx}{out_p.suffix}"
                part_file.write_text(part_content, encoding="utf-8")
        else:
            print(parts[0])
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
