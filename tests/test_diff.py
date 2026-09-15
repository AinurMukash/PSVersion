"""Unit tests for the pure canonical Diff_Engine."""

from __future__ import annotations

import json
from pathlib import Path

from enbek_monitor.diff import DiffEngine
from enbek_monitor.models import FieldLevelDiff, NormalizedRecord


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _pairs(changes: list[FieldLevelDiff]) -> list[tuple[str, object, object]]:
    return [(change.path, change.old, change.new) for change in changes]


def test_diff_reports_nested_scalar_leaf_and_omits_equal_values() -> None:
    old = {
        "profile": {"name": "old", "unchanged": None},
        "same": "value",
    }
    new = {
        "profile": {"name": "new", "unchanged": None},
        "same": "value",
    }

    changes = DiffEngine().diff(old, new)

    assert _pairs(changes) == [("$.profile.name", "old", "new")]


def test_diff_reports_collection_additions_before_removals() -> None:
    old = {
        "items": [
            {"id": 1, "name": "keep"},
            {"id": 2, "name": "remove"},
        ]
    }
    new = {
        "items": [
            {"id": 1, "name": "keep"},
            {"id": 3, "name": "add"},
        ]
    }

    changes = DiffEngine().diff(old, new)

    assert _pairs(changes) == [
        ("$.items[1]", None, {"id": 3, "name": "add"}),
        ("$.items[1]", {"id": 2, "name": "remove"}, None),
    ]


def test_synthetic_diffs_enumerate_explicit_null_leaves_in_both_directions() -> None:
    record = {
        "scalar": None,
        "nested": {"value": "text", "explicit_null": None},
        "items": [None, {"item_null": None}],
    }
    engine = DiffEngine()

    added = _pairs(engine.synthetic_new(record))
    removed = _pairs(engine.synthetic_removed(record))

    expected_paths = {
        "$.items[0]",
        "$.items[1].item_null",
        "$.nested.explicit_null",
        "$.nested.value",
        "$.scalar",
    }
    assert {path for path, _, _ in added} == expected_paths
    assert {path for path, _, _ in removed} == expected_paths
    assert all(old is None for _, old, _ in added)
    assert all(new is None for _, _, new in removed)
    assert dict((path, new) for path, _, new in added)["$.scalar"] is None
    assert dict((path, old) for path, old, _ in removed)["$.nested.explicit_null"] is None


def test_diff_canonicalizes_full_normalized_records_before_comparison() -> None:
    old_payload = json.loads(
        (FIXTURES / "normalized" / "valid" / "standard-1001.json").read_text(
            encoding="utf-8"
        )
    )
    new_payload = dict(old_payload)
    new_payload["title"] = "Изменённое название"
    old = NormalizedRecord.from_dict(old_payload)
    new = NormalizedRecord.from_dict(new_payload)

    changes = DiffEngine().diff(old, new)

    assert _pairs(changes) == [("$.title", old.title, new.title)]
