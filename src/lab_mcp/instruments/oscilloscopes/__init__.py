"""Drivers for oscilloscopes."""

from .rigol_ds4000 import MIN_SECONDS_PER_DIV as _DS4000_TIMEBASES
from .rigol_ds4000 import RigolDS4000
from .siglent_sds1000xe import IncompleteTransferError, SiglentSDS1000XE

SDS_MODELS = {"SDS1104X-E"}
DS4000_MODELS = set(_DS4000_TIMEBASES)

__all__ = ["DS4000_MODELS", "IncompleteTransferError", "RigolDS4000", "SDS_MODELS", "SiglentSDS1000XE"]
