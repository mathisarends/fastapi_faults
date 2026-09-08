"""Ergonomic RFC 9457 error contracts for FastAPI."""

from ._types import FaultConfigurationError
from .fault import Fault
from .problem import Problem

__all__ = ["Fault", "FaultConfigurationError", "Problem"]
