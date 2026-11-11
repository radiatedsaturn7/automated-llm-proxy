"""Ordered, immutable database revisions bundled with the application."""
from . import v0001, v0002, v0003, v0004, v0005, v0006, v0007, v0008, v0009

REVISIONS = ((1, v0001), (2, v0002), (3, v0003), (4, v0004), (5, v0005), (6, v0006), (7, v0007), (8, v0008), (9, v0009))
HEAD = REVISIONS[-1][0]
