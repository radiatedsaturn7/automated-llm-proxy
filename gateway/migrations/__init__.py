"""Ordered, immutable database revisions bundled with the application."""
from . import v0001, v0002, v0003, v0004

REVISIONS = ((1, v0001), (2, v0002), (3, v0003), (4, v0004))
HEAD = REVISIONS[-1][0]
