"""Pure canonical field-level diffing for normalized records.

The diff engine has no persistence or network concerns.  Inputs are first
converted to the same canonical JSON shape used by the canonical printer;
comparison then walks objects and treats arrays as collections of canonical
values.  A collection item is therefore reported once as an addition or
removal, while equal values (including explicit ``null`` values) are omitted
from an ordinary comparison.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeAlias

from .canonical_json import CanonicalJsonPrinter
from .models import FieldLevelDiff, JSONValue, NormalizedRecord


CanonicalInput: TypeAlias = NormalizedRecord | Mapping[str, Any] | None
_MISSING = object()


def _json_value(value: Any, path: str = "$") -> JSONValue:
    """Copy a generic JSON value and reject values which cannot be diffed."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} contains a non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        # Sorting is intentional for generic mappings: object key insertion
        # order is not a value and must not affect diff ordering.
        keys = sorted(value, key=lambda item: item if isinstance(item, str) else str(item))
        if any(not isinstance(key, str) for key in keys):
            raise TypeError(f"{path} contains a non-string object key")
        return {
            key: _json_value(value[key], f"{path}.{key}")
            for key in keys
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict(), path)
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_document(value: CanonicalInput) -> JSONValue:
    """Return a detached, deterministically ordered JSON representation.

    Complete normalized-record mappings go through ``CanonicalJsonPrinter`` so
    schema order and all schema checks are shared with the persistence format.
    Smaller mappings are accepted as useful pure JSON subtrees for callers
    testing the tree-diff behaviour in isolation.
    """

    if value is None:
        return None
    if isinstance(value, NormalizedRecord):
        return json.loads(CanonicalJsonPrinter().print(value).decode("utf-8"))
    if isinstance(value, Mapping):
        if set(value) >= set(NormalizedRecord.field_names()):
            return json.loads(CanonicalJsonPrinter().print(value).decode("utf-8"))
        return _json_value(value)
    raise TypeError("diff input must be a NormalizedRecord, JSON object, or null")


def _path(path: str, key: str) -> str:
    return f"{path}.{key}"


def _append_change(
    changes: list[FieldLevelDiff],
    path: str,
    old: Any,
    new: Any,
    *,
    force: bool = False,
) -> None:
    if force or old != new:
        changes.append(FieldLevelDiff(path=path, old=old, new=new))


def _leaf_diffs(
    old: Any,
    new: Any,
    path: str,
    changes: list[FieldLevelDiff],
    *,
    force: bool,
) -> None:
    """Enumerate all leaves/items for a synthetic null-to-record change."""

    if isinstance(new, Mapping):
        for key, value in new.items():
            _leaf_diffs(None, value, _path(path, key), changes, force=force)
        return
    if isinstance(old, Mapping):
        for key, value in old.items():
            _leaf_diffs(value, None, _path(path, key), changes, force=force)
        return
    if isinstance(new, list):
        for index, value in enumerate(new):
            _leaf_diffs(None, value, f"{path}[{index}]", changes, force=force)
        return
    if isinstance(old, list):
        for index, value in enumerate(old):
            _leaf_diffs(value, None, f"{path}[{index}]", changes, force=force)
        return
    _append_change(changes, path, old, new, force=force)


def _collection_diffs(
    old: list[Any],
    new: list[Any],
    path: str,
    changes: list[FieldLevelDiff],
) -> None:
    """Report multiset additions before removals, in canonical list order."""

    matched_old: set[int] = set()
    additions: list[tuple[int, Any]] = []
    for new_index, new_item in enumerate(new):
        match = next(
            (
                old_index
                for old_index, old_item in enumerate(old)
                if old_index not in matched_old and old_item == new_item
            ),
            None,
        )
        if match is None:
            additions.append((new_index, new_item))
        else:
            matched_old.add(match)

    # Requirements define additions before removals for every collection.
    for new_index, new_item in additions:
        changes.append(FieldLevelDiff(path=f"{path}[{new_index}]", old=None, new=new_item))
    for old_index, old_item in enumerate(old):
        if old_index not in matched_old:
            changes.append(FieldLevelDiff(path=f"{path}[{old_index}]", old=old_item, new=None))


def _walk(old: Any, new: Any, path: str, changes: list[FieldLevelDiff]) -> None:
    """Walk two canonical JSON values and append deterministic leaf changes."""

    if isinstance(old, Mapping) and isinstance(new, Mapping):
        # Canonical records already use schema order.  Generic mappings were
        # sorted by _json_value; the union is sorted as a defensive guarantee.
        keys = list(dict.fromkeys([*old.keys(), *new.keys()]))
        for key in keys:
            old_value = old.get(key, _MISSING)
            new_value = new.get(key, _MISSING)
            _walk(
                None if old_value is _MISSING else old_value,
                None if new_value is _MISSING else new_value,
                _path(path, key),
                changes,
            )
        return

    if isinstance(old, list) and isinstance(new, list):
        _collection_diffs(old, new, path, changes)
        return

    # A nested object or collection appearing/disappearing is expanded into
    # leaf/item changes.  This gives NEW/REMOVED-compatible paths and avoids a
    # single opaque object diff for a changed nested field.
    if isinstance(new, (Mapping, list)) and not isinstance(old, (Mapping, list)):
        _leaf_diffs(old, new, path, changes, force=False)
        return
    if isinstance(old, (Mapping, list)) and not isinstance(new, (Mapping, list)):
        _leaf_diffs(old, new, path, changes, force=False)
        return

    _append_change(changes, path, old, new)


class DiffEngine:
    """Compare canonical records without network or filesystem side effects."""

    def diff(self, old: CanonicalInput, new: CanonicalInput) -> list[FieldLevelDiff]:
        old_document = _canonical_document(old)
        new_document = _canonical_document(new)
        changes: list[FieldLevelDiff] = []

        if old is None and new is None:
            return changes
        if old is None:
            _leaf_diffs(None, new_document, "$", changes, force=True)
            return changes
        if new is None:
            _leaf_diffs(old_document, None, "$", changes, force=True)
            return changes

        _walk(old_document, new_document, "$", changes)
        return changes

    compare = diff
    calculate = diff
    compute = diff
    __call__ = diff

    def synthetic_new(self, record: CanonicalInput) -> list[FieldLevelDiff]:
        """Return the complete synthetic diff used for a NEW event."""

        return self.diff(None, record)

    def synthetic_removed(self, record: CanonicalInput) -> list[FieldLevelDiff]:
        """Return the complete synthetic diff used for a REMOVED event."""

        return self.diff(record, None)


# Design-document spelling and functional convenience aliases.
Diff_Engine = DiffEngine
CanonicalDiffEngine = DiffEngine


def diff(old: CanonicalInput, new: CanonicalInput) -> list[FieldLevelDiff]:
    return DiffEngine().diff(old, new)


def diff_records(old: CanonicalInput, new: CanonicalInput) -> list[FieldLevelDiff]:
    return diff(old, new)


__all__ = [
    "CanonicalDiffEngine",
    "DiffEngine",
    "Diff_Engine",
    "diff",
    "diff_records",
]
