"""Opt-in all-staff roster OCR.

This package is deliberately isolated from the production four-person model.
Nothing in :mod:`backend` imports it and no bot selects it by default.
"""

from .schema import ALLOWED_CODES, Annotation, Box, CellAnnotation

__all__ = ["ALLOWED_CODES", "Annotation", "Box", "CellAnnotation"]
