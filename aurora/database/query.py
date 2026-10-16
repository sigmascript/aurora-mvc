class Query:
    """Represent a model query with equality filters combined using AND.

    Filter columns are validated at construction; database access happens
    when all() or first() is called. The filter mapping is shallow-copied.
    """

    def __init__(self, model, where=None):
        self.model = model
        self.where = dict(where or {})

        self.model._validate_columns(self.where)

    def _build_select(self):
        """Build a SELECT statement with the query's equality filters."""
        table = self.model.__sql_table__

        statement = table.select()

        for name, value in self.where.items():
            statement = statement.where(table.c[name] == value)

        return statement

    def all(self):
        """Execute the query and return model instances, or an empty list."""
        database = self.model._get_database()
        statement = self._build_select()

        rows = database.fetch_all(statement)

        return [self.model._from_row(row) for row in rows]

    def first(self):
        """Execute with LIMIT 1 and return a model instance, or None.

        No explicit ordering is applied.
        """
        database = self.model._get_database()
        statement = self._build_select().limit(1)
        row = database.fetch_first(statement)

        if row is None:
            return None

        return self.model._from_row(row)
