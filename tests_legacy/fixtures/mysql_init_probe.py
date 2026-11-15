"""MySQL-compatible model used only by guarded initialization tests."""
from aurora.database import Model, column


class MySQLInitProbe(Model):
    __table__ = 'd4_mysql_init_probe'

    id = column(int, primary_key=True)
    title = column(str, length=120, nullable=False)
