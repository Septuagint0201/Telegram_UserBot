"""Release compatibility constants shared by composition and persistence.

This module is intentionally dependency-free.  A release has exactly one expected
Alembic head; production configuration, migration composition, and tests import this
value instead of maintaining independent literals.
"""

EXPECTED_SCHEMA_REVISION = "0036_worker_complete"
EXPECTED_PGVECTOR_VERSION = "0.8.6"
RESOURCE_PROFILE = "v1_2cpu_4g_minimum40_target"

__all__ = ["EXPECTED_PGVECTOR_VERSION", "EXPECTED_SCHEMA_REVISION", "RESOURCE_PROFILE"]
