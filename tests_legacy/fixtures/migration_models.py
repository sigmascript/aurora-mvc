"""Small SQLAlchemy model fixtures for database migration tests.

Keep model class names stable: they determine SQL table names used in assertions.
These models are test-only and are not part of Aurora's public API.
"""

from aurora.database import Model, column


class MigrationExample(Model):
    id = column(int, primary_key=True)
    title = column(str, nullable=True)


class ExistingProduct(Model):
    id = column(int, primary_key=True)
    title = column(str, nullable=True)
    description = column(str, nullable=True)


class InvalidProduct(Model):
    id = column(int, primary_key=True)
    title = column(str, nullable=True)
    required = column(str, nullable=False)


class ProductWithDefault(Model):
    id = column(int, primary_key=True)
    title = column(str, nullable=True)
    status = column(str, nullable=True, server_default='new')
