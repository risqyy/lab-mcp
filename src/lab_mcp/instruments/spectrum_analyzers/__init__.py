"""Drivers for spectrum analyzers."""

from .rigol_dsa800 import MAX_FREQUENCY_HZ as _DSA800_FREQUENCIES
from .rigol_dsa800 import RigolDSA800

DSA800_MODELS = set(_DSA800_FREQUENCIES)

__all__ = ["DSA800_MODELS", "RigolDSA800"]
