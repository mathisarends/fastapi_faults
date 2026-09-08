"""Ergonomic RFC 9457 error contracts for FastAPI."""

from ._types import FaultConfigurationError
from .problem import Problem

__all__ = ["FaultConfigurationError", "Problem"]
