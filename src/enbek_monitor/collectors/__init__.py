"""Collectors that retrieve source artifacts without embedding domain logic."""

from .detail import (
    DetailCollectionResult,
    DetailCollector,
    Detail_Collection_Result,
    Detail_Collector,
)
from .pdf import (
    PdfCollectionResult,
    PdfCollector,
    Pdf_Collection_Result,
    Pdf_Collector,
)
from .registry import (
    RegistryCollectionResult,
    RegistryCollector,
    Registry_Collection_Result,
    Registry_Collector,
)

__all__ = [
    "DetailCollectionResult",
    "DetailCollector",
    "Detail_Collection_Result",
    "Detail_Collector",
    "PdfCollectionResult",
    "PdfCollector",
    "Pdf_Collection_Result",
    "Pdf_Collector",
    "RegistryCollectionResult",
    "RegistryCollector",
    "Registry_Collection_Result",
    "Registry_Collector",
]
