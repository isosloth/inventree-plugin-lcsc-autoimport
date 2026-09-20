"""LCSC Auto Import InvenTree plugin."""

from .version import PLUGIN_VERSION 

from .core import LCSCAutoImport

__all__ = ["LCSCAutoImport", "PLUGIN_VERSION"]