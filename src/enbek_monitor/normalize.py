"""Pure canonical normalization for Enbek professional-standard records.

The normalizer is deliberately a boundary function: it accepts already parsed
registry/detail/PDF values, performs no network or filesystem work, and either
returns one complete :class:`NormalizedRecord` or a structured
:class:`NormalizationError`.  Source values are never inferred from external
fields; official categories are a projection of the values published in the
detail source and the supplied lifecycle context only.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime, timezone
from numbers import Real
from typing import Any, TypeAlias

from .errors import NormalizationError
from .models import (
    Categories,
    Hashes,
    LifecycleState,
    LifecycleStatus,
    NormalizedRecord,
    Number,
    OKEDAttributes,
    OfficialCategory,
    ProfessionRecord,
    RegistryAttributes,
    RegistryEntry,
    SourceAttributes,
    SourceReference,
    Timestamps,
)
from .parsers.source_schema import DetailPage


PDFDescriptor: TypeAlias = Any
LifecycleContext: TypeAlias = Any
NormalizationResult: TypeAlias = NormalizedRecord | NormalizationError

# HTML is removed while retaining text and decoding character references.  The
# parser contract already provides text in most cases, but this also handles
# direct SourceAttributes supplied by callers and malformed-but-recoverable
# markup without importing an HTML/network framework.
_TAG_RE = re.compile(r"<[^>]*>")


def _canonical_text(value: Any, field: str) -> str | None:
    """Return canonical text, mapping absent/empty values to JSON null."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or null")
    # Decode entities before removing tags so ``&lt;tag&gt;`` is represented as
    # source text, while actual markup is removed.  Whitespace is intentionally
    # not collapsed: only the required boundary trim is canonicalized.
    text = html.unescape(_TAG_RE.sub("", value))
    # HTML non-breaking spaces are presentation whitespace, not meaningful
    # source characters; use the ordinary space expected by canonical JSON.
    text = text.replace("\u00a0", " ")
    text = unicodedata.normalize("NFC", text).strip()
    return text or None


def _canonical_number(value: Any, field: str) -> Number | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{field} must be a number or null")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field} must be finite")
        # 1.0 is the same JSON number as 1 and the canonical fixtures use the
        # integer representation where no fractional value is present.
        if value.is_integer():
            return int(value)
    return value


def _canonical_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a date/datetime string or null")
    text = value.strip()
    if not text:
        return None
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid calendar date") from exc


def _canonical_datetime(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 date-time") from exc
    else:
        raise TypeError(f"{field} must be an ISO-8601 date-time or null")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_source_url(value: Any, field: str) -> str | None:
    """Preserve a published URL byte-for-byte, treating blank as missing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or null")
    return value if value.strip() else None


def _coerce_registry(value: RegistryAttributes | RegistryEntry) -> RegistryAttributes:
    if isinstance(value, RegistryAttributes):
        return value
    if isinstance(value, RegistryEntry):
        return RegistryAttributes(
            enbek_id=value.enbek_id,
            code=value.business_code,
            detail_page_url=value.detail_page_url,
            registry_url=None,
        )
    raise TypeError("registry input must be RegistryAttributes or RegistryEntry")


def _coerce_detail(value: DetailPage | SourceAttributes) -> tuple[SourceAttributes, str | None, bool | None]:
    if isinstance(value, DetailPage):
        return value.attributes, value.source_url, value.pdf_published
    if isinstance(value, SourceAttributes):
        return value, None, None
    raise TypeError("detail input must be DetailPage or SourceAttributes")


def _coerce_state(context: LifecycleContext) -> LifecycleState:
    if context is None:
        return LifecycleState.ACTIVE
    value: Any = context
    if isinstance(context, LifecycleStatus):
        value = context.state
    elif isinstance(context, Mapping):
        value = context.get("state", context.get("lifecycle_state", LifecycleState.ACTIVE))
    else:
        value = getattr(context, "state", getattr(context, "lifecycle_state", context))
    if isinstance(value, LifecycleState):
        return value
    try:
        return LifecycleState(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lifecycle state is invalid") from exc


def _coerce_timestamps(context: LifecycleContext, explicit: Any) -> Timestamps:
    value = explicit
    if value is None and isinstance(context, Mapping):
        value = context.get("timestamps")
    if value is None:
        return Timestamps()
    if isinstance(value, Timestamps):
        return Timestamps(
            first_seen_at=_canonical_datetime(value.first_seen_at, "timestamps.first_seen_at"),
            last_seen_at=_canonical_datetime(value.last_seen_at, "timestamps.last_seen_at"),
            last_changed_at=_canonical_datetime(value.last_changed_at, "timestamps.last_changed_at"),
        )
    if isinstance(value, Mapping):
        return Timestamps(
            first_seen_at=_canonical_datetime(value.get("first_seen_at"), "timestamps.first_seen_at"),
            last_seen_at=_canonical_datetime(value.get("last_seen_at"), "timestamps.last_seen_at"),
            last_changed_at=_canonical_datetime(value.get("last_changed_at"), "timestamps.last_changed_at"),
        )
    raise TypeError("timestamps must be Timestamps or an object")


def _canonical_oked(value: OKEDAttributes | None) -> OKEDAttributes | None:
    if value is None:
        return None
    if not isinstance(value, OKEDAttributes):
        raise TypeError("oked must be OKEDAttributes or null")
    return OKEDAttributes(
        **{
            name: _canonical_text(getattr(value, name), f"oked.{name}")
            for name in OKEDAttributes.__schema_fields__
        }
    )


def _canonical_professions(values: Any) -> list[ProfessionRecord]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise TypeError("professions must be a collection or null")
    result: list[ProfessionRecord] = []
    for index, item in enumerate(values):
        if not isinstance(item, ProfessionRecord):
            raise TypeError(f"professions[{index}] must be ProfessionRecord")
        level = item.ork_level
        if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
            raise TypeError(f"professions[{index}].ork_level must be an integer or null")
        result.append(
            ProfessionRecord(
                name=_canonical_text(item.name, f"professions[{index}].name"),
                ork_level=level,
            )
        )
    # Profession order is source-defined and therefore intentionally retained.
    return result


def _pdf_hash(descriptor: PDFDescriptor, expected_id: int) -> str | None:
    """Extract or calculate a PDF digest without reading files or making calls."""

    if descriptor is None:
        return None
    if isinstance(descriptor, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(descriptor)).hexdigest()
    if isinstance(descriptor, str):
        return _validate_digest(descriptor)
    if isinstance(descriptor, Mapping):
        descriptor_id = descriptor.get("enbek_id")
        if descriptor_id is not None and descriptor_id != expected_id:
            raise ValueError("PDF descriptor Enbek_ID does not match")
        for key in ("pdf_sha256", "sha256", "content_hash", "hash"):
            candidate = descriptor.get(key)
            if candidate is not None:
                return _validate_digest(candidate)
        for key in ("content", "body", "bytes"):
            if key in descriptor and descriptor[key] is not None:
                return _pdf_hash(descriptor[key], expected_id)
        return None

    descriptor_id = getattr(descriptor, "enbek_id", None)
    if descriptor_id is not None and descriptor_id != expected_id:
        raise ValueError("PDF descriptor Enbek_ID does not match")
    for key in ("pdf_sha256", "sha256", "content_hash", "hash"):
        candidate = getattr(descriptor, key, None)
        if candidate is not None:
            return _validate_digest(candidate)
    artifact = getattr(descriptor, "artifact", None)
    if artifact is not None:
        return _pdf_hash(artifact, expected_id)
    content = getattr(descriptor, "content", getattr(descriptor, "body", None))
    if content is not None:
        return _pdf_hash(content, expected_id)
    return None


def _validate_digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("PDF hash must be a 64-character hexadecimal SHA-256 digest")
    return value.lower()


class OfficialCategorizer:
    """Build ``categories.official`` from source-published values only."""

    def categorize(
        self,
        source: SourceAttributes | NormalizedRecord | None = None,
        lifecycle_state: LifecycleState | str = LifecycleState.ACTIVE,
        *,
        oked: OKEDAttributes | None = None,
        government_body: str | None = None,
        professions: list[ProfessionRecord] | tuple[ProfessionRecord, ...] | None = None,
        year: Number | None = None,
        version: Number | None = None,
    ) -> OfficialCategory:
        if source is not None:
            if isinstance(source, NormalizedRecord):
                oked = source.oked
                government_body = source.government_body
                professions = source.professions
                year = source.year
                version = source.version
            elif isinstance(source, SourceAttributes):
                oked = source.oked
                government_body = source.government_body
                professions = source.professions
                year = source.year
                version = source.version
            else:
                raise TypeError("category source must be SourceAttributes or NormalizedRecord")
        state = LifecycleState(lifecycle_state)
        canonical_oked = _canonical_oked(oked)
        # The hierarchy is schema-defined, so its order is retained.  Only
        # published non-empty codes enter the category projection.
        oked_codes = (
            [
                getattr(canonical_oked, name)
                for name in (
                    "section_code",
                    "division_code",
                    "group_code",
                    "class_code",
                    "subclass_code",
                )
                if canonical_oked is not None and getattr(canonical_oked, name) is not None
            ]
            if canonical_oked is not None
            else []
        )
        levels = sorted(
            {
                profession.ork_level
                for profession in (professions or [])
                if isinstance(profession, ProfessionRecord) and profession.ork_level is not None
            }
        )
        return OfficialCategory(
            oked=oked_codes,
            government_body=_canonical_text(government_body, "government_body"),
            ork_levels=levels,
            year=_canonical_number(year, "year"),
            version=_canonical_number(version, "version"),
            lifecycle_state=state,
        )

    categorize_official = categorize
    build = categorize
    __call__ = categorize


Official_Categorizer = OfficialCategorizer


class Normalizer:
    """Pure source-to-canonical normalizer."""

    def __init__(self, categorizer: OfficialCategorizer | None = None, *, schema_version: str = "1") -> None:
        if not isinstance(schema_version, str) or not schema_version.strip():
            raise ValueError("schema_version must be a non-empty string")
        self.schema_version = schema_version
        self.categorizer = categorizer or OfficialCategorizer()

    def normalize(
        self,
        registry: RegistryAttributes | RegistryEntry | None = None,
        detail: DetailPage | SourceAttributes | None = None,
        pdf_descriptor: PDFDescriptor = None,
        lifecycle_context: LifecycleContext = None,
        *,
        registry_attributes: RegistryAttributes | RegistryEntry | None = None,
        detail_page: DetailPage | SourceAttributes | None = None,
        detail_attributes: SourceAttributes | None = None,
        pdf: PDFDescriptor = None,
        pdf_sha256: str | None = None,
        timestamps: Any = None,
    ) -> NormalizationResult:
        registry = registry if registry is not None else registry_attributes
        detail = detail if detail is not None else detail_page
        detail = detail if detail is not None else detail_attributes
        if pdf_descriptor is None and pdf is not None:
            pdf_descriptor = pdf
        if pdf_descriptor is None and pdf_sha256 is not None:
            pdf_descriptor = {"sha256": pdf_sha256}
        try:
            if registry is None or detail is None:
                raise ValueError("registry and detail inputs are required")
            registry_value = _coerce_registry(registry)
            detail_value, detail_page_url, pdf_published = _coerce_detail(detail)
            if registry_value.enbek_id != detail_value.enbek_id:
                raise ValueError(
                    f"Enbek_ID mismatch: registry={registry_value.enbek_id}, "
                    f"detail={detail_value.enbek_id}"
                )
            enbek_id = registry_value.enbek_id
            state = _coerce_state(lifecycle_context)
            technical_timestamps = _coerce_timestamps(lifecycle_context, timestamps)
            pdf_hash = _pdf_hash(pdf_descriptor, enbek_id)

            # Registry code is preferred because it is the exact Business_Code
            # captured from the registry; detail code is a fallback only when
            # the registry omitted that optional scalar.
            code_source = registry_value.code if registry_value.code is not None else detail_value.code
            page_url = detail_value.page_url if detail_value.page_url is not None else detail_page_url
            if page_url is None:
                page_url = registry_value.detail_page_url
            registry_url = registry_value.registry_url
            if registry_url is None:
                registry_url = detail_value.registry_url

            oked = _canonical_oked(detail_value.oked)
            professions = _canonical_professions(detail_value.professions)
            government_body = _canonical_text(detail_value.government_body, "government_body")
            title = _canonical_text(detail_value.title, "title")
            description = _canonical_text(detail_value.description, "description")
            code = _canonical_text(code_source, "code")
            version = _canonical_number(detail_value.version, "version")
            year = _canonical_number(detail_value.year, "year")
            revision_date = _canonical_date(detail_value.revision_date, "revision_date")
            source = SourceReference(
                registry_url=_canonical_source_url(registry_url, "source.registry_url"),
                page_url=_canonical_source_url(page_url, "source.page_url"),
                pdf_url=_canonical_source_url(detail_value.pdf_url, "source.pdf_url"),
            )
            categories = Categories(
                official=self.categorizer.categorize(
                    oked=oked,
                    government_body=government_body,
                    professions=professions,
                    year=year,
                    version=version,
                    lifecycle_state=state,
                )
            )
            # Hashing is a separate component.  The PDF digest is safe to carry
            # through now; metadata/normalized hashes are filled by HashingService.
            return NormalizedRecord(
                schema_version=self.schema_version,
                enbek_id=enbek_id,
                code=code,
                title=title,
                government_body=government_body,
                description=description,
                version=version,
                year=year,
                revision_date=revision_date,
                oked=oked,
                professions=professions,
                categories=categories,
                source=source,
                hashes=Hashes(pdf_sha256=pdf_hash),
                timestamps=technical_timestamps,
            )
        except NormalizationError:
            raise
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # No record has been allocated before all input checks and source
            # projections succeed, so this result cannot expose partial data.
            return NormalizationError(
                str(exc),
                enbek_id=(
                    getattr(registry, "enbek_id", None)
                    if registry is not None and isinstance(getattr(registry, "enbek_id", None), int)
                    else None
                ),
                stage="normalization",
                path="$",
            )

    transform = normalize
    run = normalize
    __call__ = normalize


def normalize(*args: Any, **kwargs: Any) -> NormalizationResult:
    """Convenience function using the default :class:`Normalizer`."""

    return Normalizer().normalize(*args, **kwargs)


NormalizerResult = NormalizationResult
OfficialCategorizerResult = OfficialCategory
normalize_record = normalize

__all__ = [
    "LifecycleContext",
    "NormalizationResult",
    "Normalizer",
    "NormalizerResult",
    "OfficialCategorizer",
    "OfficialCategorizerResult",
    "Official_Categorizer",
    "PDFDescriptor",
    "normalize",
    "normalize_record",
]
