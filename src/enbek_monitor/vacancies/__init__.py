"""Weekly hh vacancy collection and normalization."""

from . import docs as docs
from . import parsers as parsers
from .collector import VacancyCollector, VacancyCollectionResult
from .analytics import summarize
from .history import diff_snapshots, load_previous, persist_snapshot
from .matching import match_records
from .models import VacancyRecord

__all__ = [
    "VacancyCollector",
    "VacancyCollectionResult",
    "VacancyRecord",
    "summarize",
    "match_records",
    "diff_snapshots",
    "load_previous",
    "persist_snapshot",
    "docs",
    "parsers",
]
