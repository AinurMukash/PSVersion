"""Manifest-backed access to the local test fixture set.

The fixture manifest is part of the test contract, not just a convenient
index.  This module validates it before exposing any fixture to a test:
fixture paths must stay below ``tests/fixtures``, kinds must be known, and the
``valid``/``invalid`` groups must declare the expected ``accept``/``reject``
outcome respectively.

The loader intentionally does not parse domain payloads.  Parsers and other
domain tests receive a typed :class:`FixtureCase` and decide how to process
its bytes.  That keeps manifest validation independent from the implementation
that is being tested while still making malformed fixture metadata fail early.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, TypeAlias


JSONValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)


class FixtureKind(str, Enum):
    """Kinds that can occur in the fixture manifest."""

    REGISTRY_PAGE = "Registry_Page"
    DETAIL_PAGE = "Detail_Page"
    PDF_DOCUMENT = "PDF_Document"
    NORMALIZED_RECORD = "Normalized_Record"
    SNAPSHOT_PAIR = "Snapshot_Pair"


class FixtureGroup(str, Enum):
    """Manifest groups with a fixed expected processing outcome."""

    VALID = "valid"
    INVALID = "invalid"


class ExpectedOutcome(str, Enum):
    """Outcomes declared by the fixture manifest."""

    ACCEPT = "accept"
    REJECT = "reject"


KNOWN_FIXTURE_KINDS: tuple[str, ...] = tuple(
    kind.value for kind in FixtureKind if kind is not FixtureKind.SNAPSHOT_PAIR
)
KNOWN_MANIFEST_KINDS: frozenset[str] = frozenset(
    (*KNOWN_FIXTURE_KINDS, FixtureKind.SNAPSHOT_PAIR.value)
)
EXPECTED_OUTCOMES: frozenset[str] = frozenset(
    outcome.value for outcome in ExpectedOutcome
)
SNAPSHOT_SCENARIOS: frozenset[str] = frozenset(
    {"NEW", "UPDATED", "UNCHANGED", "MISSING", "REMOVED", "RESTORED"}
)

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().with_name("manifest.json")
DEFAULT_FIXTURES_ROOT = DEFAULT_MANIFEST_PATH.parent


class ManifestValidationError(ValueError):
    """Raised when the fixture manifest cannot be used safely.

    ``violations`` contains stable, human-readable paths into the manifest so
    a broken fixture set can be diagnosed without executing a domain test.
    Validation collects all discoverable violations in one pass instead of
    hiding later errors behind the first malformed entry.
    """

    def __init__(self, message: str, *, violations: tuple[str, ...] = ()) -> None:
        self.violations = violations
        if violations:
            details = "\n".join(f"- {violation}" for violation in violations)
            message = f"{message}\n{details}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FixtureCase:
    """A validated, typed reference to one ordinary fixture file."""

    id: str
    kind: FixtureKind
    group: FixtureGroup
    path: Path
    relative_path: str
    expected_outcome: ExpectedOutcome
    expected_error_paths: tuple[str, ...]
    expected_error_reasons: tuple[str, ...]
    metadata: Mapping[str, JSONValue]

    @property
    def format(self) -> str | None:
        """Return the optional source format declared by the manifest."""

        value = self.metadata.get("format")
        return value if isinstance(value, str) else None

    @property
    def expected_acceptance(self) -> bool:
        """Whether this case is expected to be accepted by its domain parser."""

        return self.expected_outcome is ExpectedOutcome.ACCEPT

    def read_bytes(self) -> bytes:
        """Read the exact fixture bytes through the manifest-resolved path."""

        return self.path.read_bytes()

    def read_text(self, *, encoding: str = "utf-8") -> str:
        """Read a text fixture using an explicit encoding."""

        return self.path.read_text(encoding=encoding)

    def load_json(self, *, encoding: str = "utf-8") -> Any:
        """Decode a JSON fixture when the calling domain test needs JSON."""

        return json.loads(self.read_text(encoding=encoding))


@dataclass(frozen=True, slots=True)
class SnapshotPairCase:
    """A validated fixed old/new snapshot scenario."""

    id: str
    kind: FixtureKind
    scenario: str
    path: Path
    relative_path: str
    expected_outcome: ExpectedOutcome
    expected_error_paths: tuple[str, ...]
    expected_error_reasons: tuple[str, ...]
    metadata: Mapping[str, JSONValue]

    def read_bytes(self) -> bytes:
        """Read the exact snapshot-pair fixture bytes."""

        return self.path.read_bytes()

    def read_text(self, *, encoding: str = "utf-8") -> str:
        """Read the snapshot-pair fixture as text."""

        return self.path.read_text(encoding=encoding)

    def load_json(self, *, encoding: str = "utf-8") -> Any:
        """Decode a snapshot-pair fixture as JSON."""

        return json.loads(self.read_text(encoding=encoding))


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    """Validated manifest and typed accessors for all local fixtures."""

    manifest_path: Path
    fixtures_root: Path
    schema_version: str
    fixture_set: str
    network_required: bool
    cases: Mapping[str, Mapping[str, tuple[FixtureCase, ...]]]
    snapshot_pairs: tuple[SnapshotPairCase, ...]
    fallback: Mapping[str, JSONValue]
    raw: Mapping[str, JSONValue]

    @property
    def path(self) -> Path:
        """Alias for the manifest file path."""

        return self.manifest_path

    @property
    def all_cases(self) -> tuple[FixtureCase, ...]:
        """Return every ordinary fixture in manifest declaration order."""

        return tuple(
            case
            for kind in KNOWN_FIXTURE_KINDS
            for group in (FixtureGroup.VALID.value, FixtureGroup.INVALID.value)
            for case in self.cases.get(kind, {}).get(group, ())
        )

    @property
    def cases_by_kind(self) -> Mapping[str, tuple[FixtureCase, ...]]:
        """Return ordinary cases grouped only by their source kind."""

        return MappingProxyType(
            {
                kind: tuple(
                    case
                    for group in (FixtureGroup.VALID.value, FixtureGroup.INVALID.value)
                    for case in self.cases.get(kind, {}).get(group, ())
                )
                for kind in KNOWN_FIXTURE_KINDS
            }
        )

    @property
    def valid_cases(self) -> tuple[FixtureCase, ...]:
        """Return all cases declared in the valid groups."""

        return tuple(
            case
            for kind in KNOWN_FIXTURE_KINDS
            for case in self.cases.get(kind, {}).get(FixtureGroup.VALID.value, ())
        )

    @property
    def invalid_cases(self) -> tuple[FixtureCase, ...]:
        """Return all cases declared in the invalid groups."""

        return tuple(
            case
            for kind in KNOWN_FIXTURE_KINDS
            for case in self.cases.get(kind, {}).get(FixtureGroup.INVALID.value, ())
        )

    def get_cases(
        self,
        kind: FixtureKind | str,
        *,
        group: FixtureGroup | str | None = None,
        expected_outcome: ExpectedOutcome | str | None = None,
    ) -> tuple[FixtureCase, ...]:
        """Return cases through the validated manifest API.

        ``group`` and ``expected_outcome`` are optional filters.  If both are
        supplied they must agree; this catches accidental use of a valid case
        as a rejection fixture (and vice versa) in the test itself.
        """

        kind_value = _enum_value(kind, FixtureKind, "fixture kind")
        if kind_value is FixtureKind.SNAPSHOT_PAIR:
            raise ValueError("Snapshot_Pair cases are available via snapshot_pairs")

        group_value: str | None = None
        if group is not None:
            group_value = _enum_value(group, FixtureGroup, "fixture group").value
        outcome_value: str | None = None
        if expected_outcome is not None:
            outcome_value = _enum_value(
                expected_outcome, ExpectedOutcome, "expected outcome"
            ).value

        if group_value is None:
            selected = self.cases.get(kind_value.value, {}).values()
            result = tuple(case for cases in selected for case in cases)
        else:
            result = self.cases.get(kind_value.value, {}).get(group_value, ())

        if outcome_value is not None:
            result = tuple(
                case for case in result if case.expected_outcome.value == outcome_value
            )
        return result

    def iter_cases(
        self,
        kind: FixtureKind | str | None = None,
        *,
        group: FixtureGroup | str | None = None,
    ) -> Iterator[FixtureCase]:
        """Iterate validated cases without exposing raw manifest dictionaries."""

        kinds = (
            (kind,)
            if kind is not None
            else tuple(FixtureKind(value) for value in KNOWN_FIXTURE_KINDS)
        )
        for selected_kind in kinds:
            yield from self.get_cases(selected_kind, group=group)

    def get_case(self, kind: FixtureKind | str, case_id: str) -> FixtureCase:
        """Look up one ordinary fixture by kind and manifest id."""

        case_id = _query_text(case_id, "fixture id")
        for case in self.get_cases(kind):
            if case.id == case_id:
                return case
        raise KeyError(f"unknown fixture {FixtureKind(kind).value!r}/{case_id!r}")

    # Short alias useful in parameterized tests.
    case = get_case

    def get_snapshot_pair(self, scenario_or_id: str) -> SnapshotPairCase:
        """Look up a fixed snapshot pair by id or lifecycle scenario."""

        value = _query_text(scenario_or_id, "snapshot pair id")
        for pair in self.snapshot_pairs:
            if pair.id == value or pair.scenario == value:
                return pair
        raise KeyError(f"unknown snapshot pair {value!r}")

    def resolve(self, relative_path: str) -> Path:
        """Resolve a manifest-relative path while enforcing the fixture root."""

        violations: list[str] = []
        resolved = _resolve_fixture_path(
            self.fixtures_root,
            relative_path,
            "path",
            violations,
            require_file=False,
        )
        if violations or resolved is None:
            raise ManifestValidationError(
                "fixture path is outside the manifest root",
                violations=tuple(violations),
            )
        return resolved


def load_manifest(
    manifest_path: str | PathLike[str] | Path | None = None,
    *,
    fixtures_root: str | PathLike[str] | Path | None = None,
) -> FixtureManifest:
    """Load and fully validate a fixture manifest from disk.

    The default points at the repository's ``tests/fixtures/manifest.json``.
    ``fixtures_root`` is mainly useful for unit tests that create a temporary
    manifest; all paths in the document remain relative to that root.
    """

    path = Path(manifest_path or DEFAULT_MANIFEST_PATH).expanduser()
    if not path.exists():
        raise ManifestValidationError(
            "fixture manifest does not exist",
            violations=(f"manifest: missing file {path}",),
        )
    if not path.is_file():
        raise ManifestValidationError(
            "fixture manifest is not a file",
            violations=(f"manifest: expected file, got {path}",),
        )

    path = path.resolve()
    root = Path(fixtures_root or path.parent).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, JSONDecodeError) as exc:
        raise ManifestValidationError(
            "fixture manifest cannot be decoded as UTF-8 JSON",
            violations=(f"$: {exc}",),
        ) from exc

    return validate_manifest(document, fixtures_root=root, manifest_path=path)


def load_fixture_manifest(
    manifest_path: str | PathLike[str] | Path | None = None,
    *,
    fixtures_root: str | PathLike[str] | Path | None = None,
) -> FixtureManifest:
    """Explicit alias for callers that prefer the longer function name."""

    return load_manifest(manifest_path, fixtures_root=fixtures_root)


def validate_manifest(
    document: Mapping[str, Any] | str | PathLike[str] | Path,
    *,
    fixtures_root: str | PathLike[str] | Path | None = None,
    manifest_path: str | PathLike[str] | Path | None = None,
) -> FixtureManifest:
    """Validate a decoded manifest or load one when ``document`` is a path.

    Validation is intentionally independent from pytest.  A malformed
    temporary manifest therefore raises the same ``ManifestValidationError``
    whether it is checked by a unit test or by ``tests/conftest.py`` during
    pytest startup.
    """

    if isinstance(document, (str, PathLike, Path)):
        return load_manifest(document, fixtures_root=fixtures_root)
    if not isinstance(document, Mapping):
        raise ManifestValidationError(
            "fixture manifest must be a JSON object",
            violations=("$: expected object",),
        )

    root = Path(fixtures_root or DEFAULT_FIXTURES_ROOT).expanduser().resolve()
    path = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else root / "manifest.json"
    )
    violations: list[str] = []

    if not root.exists() or not root.is_dir():
        violations.append(f"fixtures_root: directory does not exist: {root}")

    schema_version = _required_text_value(
        document.get("schema_version"), "$.schema_version", violations
    )
    fixture_set = _required_text_value(
        document.get("fixture_set"), "$.fixture_set", violations
    )
    network_required = document.get("network_required")
    if not isinstance(network_required, bool):
        violations.append("$.network_required: expected boolean")
        network_required = False

    expected_outcomes = _string_list(
        document.get("expected_outcome_values"),
        "$.expected_outcome_values",
        violations,
        required=True,
    )
    if expected_outcomes is not None:
        unknown_outcomes = sorted(set(expected_outcomes) - EXPECTED_OUTCOMES)
        if unknown_outcomes:
            violations.append(
                "$.expected_outcome_values: unknown outcome(s) "
                + ", ".join(repr(value) for value in unknown_outcomes)
            )
        if set(expected_outcomes) != EXPECTED_OUTCOMES:
            violations.append(
                "$.expected_outcome_values: must declare exactly 'accept' and 'reject'"
            )

    min_count, max_count = _validate_count_constraints(
        document.get("fixture_count_constraints"), violations
    )

    raw_fixtures = document.get("fixtures")
    if not isinstance(raw_fixtures, Mapping):
        violations.append("$.fixtures: expected object")
        raw_fixtures = {}

    unknown_kinds = [
        str(kind) for kind in raw_fixtures if str(kind) not in KNOWN_FIXTURE_KINDS
    ]
    for kind in unknown_kinds:
        violations.append(f"$.fixtures.{kind}: unknown fixture kind")

    cases: dict[str, dict[str, tuple[FixtureCase, ...]]] = {}
    seen_ids: set[str] = set()
    for kind in KNOWN_FIXTURE_KINDS:
        if kind not in raw_fixtures:
            violations.append(f"$.fixtures.{kind}: missing required fixture kind")
            cases[kind] = {group.value: () for group in FixtureGroup}
            continue

        raw_groups = raw_fixtures[kind]
        if not isinstance(raw_groups, Mapping):
            violations.append(f"$.fixtures.{kind}: expected object with valid/invalid groups")
            cases[kind] = {group.value: () for group in FixtureGroup}
            continue

        unknown_groups = [
            str(group)
            for group in raw_groups
            if str(group) not in {group.value for group in FixtureGroup}
        ]
        for group in unknown_groups:
            violations.append(f"$.fixtures.{kind}.{group}: unknown fixture group")

        group_cases: dict[str, tuple[FixtureCase, ...]] = {}
        for group in FixtureGroup:
            group_name = group.value
            group_path = f"$.fixtures.{kind}.{group_name}"
            raw_entries = raw_groups.get(group_name)
            if not isinstance(raw_entries, list):
                violations.append(f"{group_path}: expected array")
                group_cases[group_name] = ()
                continue

            if not min_count <= len(raw_entries) <= max_count:
                violations.append(
                    f"{group_path}: expected between {min_count} and {max_count} "
                    f"fixtures, got {len(raw_entries)}"
                )

            parsed: list[FixtureCase] = []
            for index, raw_entry in enumerate(raw_entries):
                case = _parse_fixture_case(
                    raw_entry,
                    kind=kind,
                    group=group,
                    index=index,
                    root=root,
                    manifest_path=group_path,
                    seen_ids=seen_ids,
                    violations=violations,
                )
                if case is not None:
                    parsed.append(case)
            group_cases[group_name] = tuple(parsed)
        cases[kind] = group_cases

    fallback = _validate_fallback(document.get("fallback"), root, violations)
    snapshot_pairs = _validate_snapshot_pairs(
        document.get("snapshot_pairs"), root, seen_ids, violations
    )

    if violations:
        raise ManifestValidationError(
            "fixture manifest validation failed",
            violations=tuple(violations),
        )

    # The validation above guarantees these values are present and correctly
    # typed; the casts keep the public model precise without runtime packages.
    assert schema_version is not None
    assert fixture_set is not None
    return FixtureManifest(
        manifest_path=path,
        fixtures_root=root,
        schema_version=schema_version,
        fixture_set=fixture_set,
        network_required=network_required,
        cases=MappingProxyType(
            {
                kind: MappingProxyType(dict(groups))
                for kind, groups in cases.items()
            }
        ),
        snapshot_pairs=tuple(snapshot_pairs),
        fallback=MappingProxyType(fallback),
        raw=MappingProxyType(copy.deepcopy(dict(document))),
    )


def validate_fixture_manifest(
    document: Mapping[str, Any] | str | PathLike[str] | Path,
    *,
    fixtures_root: str | PathLike[str] | Path | None = None,
    manifest_path: str | PathLike[str] | Path | None = None,
) -> FixtureManifest:
    """Explicit alias for :func:`validate_manifest`."""

    return validate_manifest(
        document,
        fixtures_root=fixtures_root,
        manifest_path=manifest_path,
    )


def _parse_fixture_case(
    raw_entry: Any,
    *,
    kind: str,
    group: FixtureGroup,
    index: int,
    root: Path,
    manifest_path: str,
    seen_ids: set[str],
    violations: list[str],
) -> FixtureCase | None:
    entry_path = f"{manifest_path}[{index}]"
    if not isinstance(raw_entry, Mapping):
        violations.append(f"{entry_path}: expected object")
        return None

    case_id = _required_text_value(raw_entry.get("id"), f"{entry_path}.id", violations)
    if case_id is not None:
        if case_id in seen_ids:
            violations.append(f"{entry_path}.id: duplicate fixture id {case_id!r}")
        else:
            seen_ids.add(case_id)

    raw_kind = _required_text_value(
        raw_entry.get("kind"), f"{entry_path}.kind", violations
    )
    if raw_kind is not None:
        if raw_kind not in KNOWN_MANIFEST_KINDS:
            violations.append(f"{entry_path}.kind: unknown fixture kind {raw_kind!r}")
        elif raw_kind != kind:
            violations.append(
                f"{entry_path}.kind: must equal containing kind {kind!r}, got {raw_kind!r}"
            )

    raw_outcome = _required_text_value(
        raw_entry.get("expected_outcome"),
        f"{entry_path}.expected_outcome",
        violations,
    )
    if raw_outcome is not None and raw_outcome not in EXPECTED_OUTCOMES:
        violations.append(
            f"{entry_path}.expected_outcome: must be 'accept' or 'reject', got {raw_outcome!r}"
        )
    expected_group_outcome = (
        ExpectedOutcome.ACCEPT.value
        if group is FixtureGroup.VALID
        else ExpectedOutcome.REJECT.value
    )
    if raw_outcome is not None and raw_outcome != expected_group_outcome:
        violations.append(
            f"{entry_path}.expected_outcome: group {group.value!r} requires "
            f"{expected_group_outcome!r}"
        )

    relative_path_value = _required_text_value(
        raw_entry.get("path"), f"{entry_path}.path", violations
    )
    resolved_path = None
    if relative_path_value is not None:
        resolved_path = _resolve_fixture_path(
            root,
            relative_path_value,
            f"{entry_path}.path",
            violations,
            require_file=True,
        )

    expected_error_paths = _string_list(
        raw_entry.get("expected_error_paths"),
        f"{entry_path}.expected_error_paths",
        violations,
        required=True,
    )
    expected_error_reasons = _string_list(
        raw_entry.get("expected_error_reasons"),
        f"{entry_path}.expected_error_reasons",
        violations,
        required=False,
    )

    # ``body_path`` is a secondary fixture path used by PDF descriptors.  A
    # deliberately invalid descriptor may omit it or set it to null, so only
    # a declared string is treated as a path to validate.
    body_path = raw_entry.get("body_path")
    if isinstance(body_path, str):
        _resolve_fixture_path(
            root,
            body_path,
            f"{entry_path}.body_path",
            violations,
            require_file=True,
        )

    if (
        case_id is None
        or raw_kind != kind
        or raw_outcome not in EXPECTED_OUTCOMES
        or resolved_path is None
        or expected_error_paths is None
        or expected_error_reasons is None
    ):
        return None

    return FixtureCase(
        id=case_id,
        kind=FixtureKind(kind),
        group=group,
        path=resolved_path,
        relative_path=relative_path_value,
        expected_outcome=ExpectedOutcome(raw_outcome),
        expected_error_paths=tuple(expected_error_paths),
        expected_error_reasons=tuple(expected_error_reasons),
        metadata=MappingProxyType(copy.deepcopy(dict(raw_entry))),
    )


def _validate_snapshot_pairs(
    raw_pairs: Any,
    root: Path,
    seen_ids: set[str],
    violations: list[str],
) -> list[SnapshotPairCase]:
    """Validate optional fixed lifecycle pair entries.

    The ordinary four fixture kinds are the required part of task 2.1.  Pair
    entries are validated when present so the existing fixture set is fully
    checked, while a smaller manifest used by a focused loader unit test can
    still exercise the ordinary contract without inventing pair files.
    """

    if raw_pairs is None:
        return []
    if not isinstance(raw_pairs, list):
        violations.append("$.snapshot_pairs: expected array")
        return []

    pairs: list[SnapshotPairCase] = []
    scenarios: set[str] = set()
    for index, raw_pair in enumerate(raw_pairs):
        entry_path = f"$.snapshot_pairs[{index}]"
        if not isinstance(raw_pair, Mapping):
            violations.append(f"{entry_path}: expected object")
            continue

        pair_id = _required_text_value(raw_pair.get("id"), f"{entry_path}.id", violations)
        if pair_id is not None:
            if pair_id in seen_ids:
                violations.append(f"{entry_path}.id: duplicate fixture id {pair_id!r}")
            else:
                seen_ids.add(pair_id)

        raw_kind = _required_text_value(
            raw_pair.get("kind"), f"{entry_path}.kind", violations
        )
        if raw_kind != FixtureKind.SNAPSHOT_PAIR.value:
            violations.append(
                f"{entry_path}.kind: must be {FixtureKind.SNAPSHOT_PAIR.value!r}"
            )

        scenario = _required_text_value(
            raw_pair.get("scenario"), f"{entry_path}.scenario", violations
        )
        if scenario is not None:
            if scenario not in SNAPSHOT_SCENARIOS:
                violations.append(f"{entry_path}.scenario: unknown scenario {scenario!r}")
            elif scenario in scenarios:
                violations.append(f"{entry_path}.scenario: duplicate scenario {scenario!r}")
            else:
                scenarios.add(scenario)

        outcome = _required_text_value(
            raw_pair.get("expected_outcome"),
            f"{entry_path}.expected_outcome",
            violations,
        )
        if outcome != ExpectedOutcome.ACCEPT.value:
            violations.append(
                f"{entry_path}.expected_outcome: snapshot pairs must declare 'accept'"
            )

        relative_path = _required_text_value(
            raw_pair.get("path"), f"{entry_path}.path", violations
        )
        resolved_path = None
        if relative_path is not None:
            resolved_path = _resolve_fixture_path(
                root,
                relative_path,
                f"{entry_path}.path",
                violations,
                require_file=True,
            )

        error_paths = _string_list(
            raw_pair.get("expected_error_paths"),
            f"{entry_path}.expected_error_paths",
            violations,
            required=True,
        )
        error_reasons = _string_list(
            raw_pair.get("expected_error_reasons"),
            f"{entry_path}.expected_error_reasons",
            violations,
            required=False,
        )

        if (
            pair_id is None
            or raw_kind != FixtureKind.SNAPSHOT_PAIR.value
            or scenario not in SNAPSHOT_SCENARIOS
            or outcome != ExpectedOutcome.ACCEPT.value
            or resolved_path is None
            or error_paths is None
            or error_reasons is None
        ):
            continue

        pairs.append(
            SnapshotPairCase(
                id=pair_id,
                kind=FixtureKind.SNAPSHOT_PAIR,
                scenario=scenario,
                path=resolved_path,
                relative_path=relative_path,
                expected_outcome=ExpectedOutcome.ACCEPT,
                expected_error_paths=tuple(error_paths),
                expected_error_reasons=tuple(error_reasons),
                metadata=MappingProxyType(copy.deepcopy(dict(raw_pair))),
            )
        )

    return pairs


def _validate_fallback(
    raw_fallback: Any,
    root: Path,
    violations: list[str],
) -> dict[str, JSONValue]:
    """Validate path-bearing fallback references without parsing the profile."""

    if raw_fallback is None:
        return {}
    if not isinstance(raw_fallback, Mapping):
        violations.append("$.fallback: expected object")
        return {}

    fallback = copy.deepcopy(dict(raw_fallback))
    path_fields = ("profile_path",)
    list_path_fields = (
        "registry_page_paths",
        "normalized_record_paths",
    )
    mapping_path_fields = (
        "detail_paths",
        "pdf_response_paths",
    )

    for field_name in path_fields:
        value = raw_fallback.get(field_name)
        if value is not None:
            _resolve_fixture_path(
                root,
                value,
                f"$.fallback.{field_name}",
                violations,
                require_file=True,
            )

    for field_name in list_path_fields:
        value = raw_fallback.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list):
            violations.append(f"$.fallback.{field_name}: expected array")
            continue
        for index, item in enumerate(value):
            _resolve_fixture_path(
                root,
                item,
                f"$.fallback.{field_name}[{index}]",
                violations,
                require_file=True,
            )

    for field_name in mapping_path_fields:
        value = raw_fallback.get(field_name)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            violations.append(f"$.fallback.{field_name}: expected object")
            continue
        for key, item in value.items():
            _resolve_fixture_path(
                root,
                item,
                f"$.fallback.{field_name}.{key}",
                violations,
                require_file=True,
            )

    return fallback


def _validate_count_constraints(
    raw_constraints: Any,
    violations: list[str],
) -> tuple[int, int]:
    """Return the allowed count range, enforcing the documented 1..10 bounds."""

    if raw_constraints is None:
        return 1, 10
    if not isinstance(raw_constraints, Mapping):
        violations.append("$.fixture_count_constraints: expected object")
        return 1, 10

    minimum_valid = raw_constraints.get("minimum_valid_per_kind")
    maximum_valid = raw_constraints.get("maximum_valid_per_kind")
    minimum_invalid = raw_constraints.get("minimum_invalid_per_kind")
    maximum_invalid = raw_constraints.get("maximum_invalid_per_kind")
    values = {
        "minimum_valid_per_kind": minimum_valid,
        "maximum_valid_per_kind": maximum_valid,
        "minimum_invalid_per_kind": minimum_invalid,
        "maximum_invalid_per_kind": maximum_invalid,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            violations.append(f"$.fixture_count_constraints.{name}: expected integer")
        elif not 1 <= value <= 10:
            violations.append(f"$.fixture_count_constraints.{name}: must be between 1 and 10")

    if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
        return 1, 10

    if minimum_valid != 1 or minimum_invalid != 1:
        violations.append(
            "$.fixture_count_constraints: minimum valid/invalid counts must be 1"
        )
    if maximum_valid != 10 or maximum_invalid != 10:
        violations.append(
            "$.fixture_count_constraints: maximum valid/invalid counts must be 10"
        )
    if minimum_valid > maximum_valid or minimum_invalid > maximum_invalid:
        violations.append("$.fixture_count_constraints: minimum cannot exceed maximum")
    return int(minimum_valid), int(maximum_valid)


def _resolve_fixture_path(
    root: Path,
    value: Any,
    label: str,
    violations: list[str],
    *,
    require_file: bool,
) -> Path | None:
    """Resolve one relative path and reject traversal/symlink escapes."""

    if not isinstance(value, str) or not value.strip():
        violations.append(f"{label}: expected non-empty relative path")
        return None

    relative = Path(value)
    if relative.is_absolute():
        violations.append(f"{label}: path must be relative to tests/fixtures")
        return None

    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        violations.append(f"{label}: path escapes fixtures root")
        return None

    if require_file and not candidate.exists():
        violations.append(f"{label}: fixture file does not exist: {value!r}")
        return None
    if require_file and not candidate.is_file():
        violations.append(f"{label}: fixture path is not a regular file: {value!r}")
        return None
    return candidate


def _required_text_value(
    value: Any,
    label: str,
    violations: list[str],
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        violations.append(f"{label}: expected non-empty string")
        return None
    return value


def _query_text(value: Any, label: str) -> str:
    """Validate a lookup argument outside manifest-document validation."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(
    value: Any,
    label: str,
    violations: list[str],
    *,
    required: bool,
) -> list[str] | None:
    if value is None:
        if required:
            violations.append(f"{label}: expected array of non-empty strings")
            return None
        return []
    if not isinstance(value, list):
        violations.append(f"{label}: expected array of non-empty strings")
        return None

    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            violations.append(f"{label}[{index}]: expected non-empty string")
        else:
            result.append(item)
    return result


def _enum_value(value: Any, enum_type: type[Enum], label: str) -> Enum:
    """Convert a public string/enum value for manifest query helpers."""

    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(repr(member.value) for member in enum_type)
        raise ValueError(f"{label} must be one of {choices}") from exc


__all__ = [
    "DEFAULT_FIXTURES_ROOT",
    "DEFAULT_MANIFEST_PATH",
    "EXPECTED_OUTCOMES",
    "ExpectedOutcome",
    "FixtureCase",
    "FixtureGroup",
    "FixtureKind",
    "FixtureManifest",
    "KNOWN_FIXTURE_KINDS",
    "KNOWN_MANIFEST_KINDS",
    "ManifestValidationError",
    "SNAPSHOT_SCENARIOS",
    "SnapshotPairCase",
    "load_fixture_manifest",
    "load_manifest",
    "validate_fixture_manifest",
    "validate_manifest",
]
