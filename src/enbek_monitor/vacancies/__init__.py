"""Weekly hh vacancy collection and normalization."""

from .collector import VacancyCollector, VacancyCollectionResult
from .analytics import summarize
from .history import diff_snapshots, load_previous, persist_snapshot
from .matching import match_records
from .models import VacancyRecord
from . import parsers

__all__ = [
    "VacancyCollector",
    "VacancyCollectionResult",
    "VacancyRecord",
    "summarize",
    "match_records",
    "diff_snapshots",
    "load_previous",
    "persist_snapshot",
    "parsers",
]
