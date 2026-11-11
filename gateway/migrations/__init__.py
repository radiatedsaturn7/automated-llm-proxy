"""Ordered, immutable database revisions bundled with the application."""
from . import v0001, v0002

REVISIONS = ((1, v0001), (2, v0002))
HEAD = REVISIONS[-1][0]
