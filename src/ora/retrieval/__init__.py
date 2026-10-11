"""Retrieval normalization, durable receipts and explicit live discovery routes."""

from .discovery import available_routes, run_discovery_page
from .normalize import RetrievalFormatError, normalize_response
from .receipts import record_delivery

__all__ = ["RetrievalFormatError", "normalize_response", "record_delivery",
           "available_routes", "run_discovery_page"]
