"""Ordered, immutable database revisions bundled with the application."""
from . import v0001

REVISIONS = ((1, v0001),)
HEAD = REVISIONS[-1][0]
