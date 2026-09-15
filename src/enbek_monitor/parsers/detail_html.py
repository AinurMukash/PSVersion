"""Pure HTML adapter for the captured Enbek detail-page contract."""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from ..errors import ParseError
from ..network import SourceResponse
from .detail_mapping import map_detail_values
from .source_schema import (
    DETAIL_FIELD_PATHS,
    DetailPage,
    HtmlResponseWrapper,
    ResponseFormat,
    SourceSchemaViolation,
    SourceFieldState,
    build_parse_error,
)


_ARTICLE_CLASS = "professional-standard"
_NUMBER_FIELDS = {"version", "year"}
_OKED_ATTRIBUTE_NAMES = (
    "section_code",
    "section_name",
    "division_code",
    "division_name",
    "group_code",
    "group_name",
    "class_code",
    "class_name",
    "subclass_code",
    "subclass_name",
)


class _DetailHTMLDocument(HTMLParser):
    """Extract only the documented ``article``/``data-field`` contract."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_attrs: dict[str, str] | None = None
        self._article_depth = 0
        self._field: str | None = None
        self._field_depth = 0
        self._field_text: dict[str, list[str]] = {}
        self._oked_attrs: dict[str, str] = {}
        self._profession_values: list[dict[str, Any]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {name: value for name, value in attrs if value is not None}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attributes(attrs)
        if tag == "article" and self.article_attrs is None:
            classes = set(attributes.get("class", "").split())
            if _ARTICLE_CLASS in classes:
                self.article_attrs = {
                    key[5:]: value
                    for key, value in attributes.items()
                    if key.startswith("data-")
                }
                self._article_depth = 1
                return

        if self.article_attrs is None:
            return
        if self._article_depth:
            self._article_depth += 1

        data_field = attributes.get("data-field")
        if data_field in {"description", "version", "year", "revision_date"}:
            self._field = data_field
            self._field_depth = self._article_depth
            self._field_text.setdefault(data_field, [])
        elif data_field == "oked":
            self._oked_attrs = {
                name: attributes[f"data-{name.replace('_', '-')}"]
                for name in _OKED_ATTRIBUTE_NAMES
                if f"data-{name.replace('_', '-')}" in attributes
            }
        elif data_field == "professions":
            # The list itself carries no value; its child li elements do.
            pass
        elif tag == "li" and "data-name" in attributes:
            self._profession_values.append(
                {
                    "name": attributes.get("data-name"),
                    "ork_level": _parse_number(attributes.get("data-ork-level")),
                }
            )

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._field_text[self._field].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.article_attrs is None or not self._article_depth:
            return
        if self._field is not None and self._article_depth == self._field_depth:
            self._field = None
            self._field_depth = 0
        self._article_depth -= 1

    def values(self) -> dict[str, Any]:
        if self.article_attrs is None:
            return {}
        values: dict[str, Any] = {
            "enbek_id": _parse_number(self.article_attrs.get("enbek-id")),
            "code": self.article_attrs.get("code"),
            "title": self.article_attrs.get("title"),
            "government_body": self.article_attrs.get("government-body"),
            "description": _text_value(self._field_text, "description"),
            "version": _parse_number(_text_value(self._field_text, "version")),
            "year": _parse_number(_text_value(self._field_text, "year")),
            "revision_date": _text_value(self._field_text, "revision_date"),
            "oked": self._oked_attrs if self._oked_attrs else None,
            "professions": self._profession_values,
            "pdf_url": self.article_attrs.get("pdf-url"),
            "pdf_published": _parse_bool(self.article_attrs.get("pdf-published")),
        }
        return values


def _text_value(fields: Mapping[str, list[str]], name: str) -> str | None:
    if name not in fields:
        return None
    return "".join(fields[name]).strip()


def _parse_number(value: str | None) -> int | float | str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return ""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return value


def _parse_bool(value: str | None) -> bool | str | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


class DetailHtmlParser:
    """Parse the fixed HTML data-attribute/selector contract without a browser."""

    def parse(self, url: str, response: SourceResponse) -> DetailPage | ParseError:
        try:
            wrapped = HtmlResponseWrapper.from_response(response, source_url=url)
        except ParseError as error:
            return error

        document = _DetailHTMLDocument()
        try:
            document.feed(wrapped.text)
            document.close()
        except (ValueError, AssertionError) as exc:
            return ParseError(
                f"malformed detail HTML: {exc}",
                url=wrapped.source_url,
                path="$",
                stage="detail_parser",
                invalid_fields=("$",),
            )

        if document.article_attrs is None:
            return build_parse_error(
                wrapped.source_url,
                [
                    SourceSchemaViolation(
                        DETAIL_FIELD_PATHS.html_root,
                        "professional-standard article is required",
                        SourceFieldState.ABSENT,
                    )
                ],
                stage="detail_parser",
            )

        return map_detail_values(
            document.values(),
            wrapped.source_url,
            response_format=ResponseFormat.HTML,
            paths=DETAIL_FIELD_PATHS,
            response=response,
        )

    __call__ = parse


DetailHTMLParser = DetailHtmlParser
HTMLDetailParser = DetailHtmlParser
DetailParser = DetailHtmlParser
parse_detail_html = DetailHtmlParser().parse

__all__ = [
    "DetailHtmlParser",
    "DetailHTMLParser",
    "HTMLDetailParser",
    "DetailParser",
    "parse_detail_html",
]
