"""Disposable offline model for restricted nullable-column compilation tests."""
from aurora.database import Model, column


class MySQLAddColumnProbe(Model):
    __table__ = "a3_nullable_column_probe"

    id = column(int, primary_key=True)
    description = column(str, length=120, nullable=True)
