"""Standalone model for database CLI and routing regression tests.

The unique table name prevents collisions with application and migration fixtures.
"""

from aurora.database import Model, column


class DatabaseProbe(Model):
    __table__ = "d4_database_probe"

    id = column(int, primary_key=True)
    title = column(str, nullable=False)
    content = column(str, nullable=False)
    user_id = column(int, nullable=False)
