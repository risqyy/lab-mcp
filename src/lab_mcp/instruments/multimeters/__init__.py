"""Drivers for digital multimeters."""

from .rigol_dm3058 import FUNCTIONS as RIGOL_DM3058_FUNCTIONS
from .rigol_dm3058 import RigolDM3058

DM3058_MODELS = {"DM3058", "DM3058E"}

__all__ = ["RigolDM3058", "RIGOL_DM3058_FUNCTIONS", "DM3058_MODELS"]
