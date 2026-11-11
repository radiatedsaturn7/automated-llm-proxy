"""Explicit prompt registry and metadata-only offline evaluation reports."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, MetaData, String, Table, Text, UniqueConstraint

from . import v0002

metadata = MetaData()
for table in v0002.metadata.sorted_tables:
    table.to_metadata(metadata)
Table("prompt_templates", metadata,
      Column("id", String(36), primary_key=True), Column("name", String(120), nullable=False, unique=True),
      Column("archived", Boolean, nullable=False), Column("created_by", String(36), nullable=False),
      Column("created_at", DateTime(timezone=True), nullable=False))
versions = Table("prompt_versions", metadata,
      Column("id", String(36), primary_key=True),
      Column("template_id", String(36), ForeignKey("prompt_templates.id"), nullable=False),
      Column("version", Integer, nullable=False), Column("content", Text, nullable=False),
      Column("variables", JSON, nullable=False), Column("created_by", String(36), nullable=False),
      Column("created_at", DateTime(timezone=True), nullable=False), UniqueConstraint("template_id", "version"))
Index("ix_prompt_versions_template_id", versions.c.template_id)
runs = Table("evaluation_runs", metadata,
      Column("id", String(36), primary_key=True),
      Column("prompt_version_id", String(36), ForeignKey("prompt_versions.id"), nullable=False),
      Column("rules", JSON, nullable=False), Column("report", JSON, nullable=False),
      Column("created_by", String(36), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))
Index("ix_evaluation_runs_prompt_version_id", runs.c.prompt_version_id)


def upgrade(connection):
    for name in ("prompt_templates", "prompt_versions", "evaluation_runs"):
        metadata.tables[name].create(connection)
