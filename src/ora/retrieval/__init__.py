"""Offline retrieval normalization and durable, source-agnostic receipts."""

from .normalize import RetrievalFormatError, normalize_response
from .receipts import record_delivery

__all__ = ["RetrievalFormatError", "normalize_response", "record_delivery"]
