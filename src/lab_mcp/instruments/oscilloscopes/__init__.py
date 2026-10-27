"""Drivers for oscilloscopes."""

from .siglent_sds1000xe import IncompleteTransferError, SiglentSDS1000XE

SDS_MODELS = {"SDS1104X-E"}

__all__ = ["IncompleteTransferError", "SDS_MODELS", "SiglentSDS1000XE"]
