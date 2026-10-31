# AuroraSQL Alpha 8 — direct API and compatibility

AuroraSQL is experimental. The existing SQLAlchemy-backed Aurora MVC database
implementation remains the default. No database CLI or migration backend has
been replaced.

## Direct Model operations

- `Model.read(where=None, *, order_by=None, limit=None, offset=0)` and
  `Model.using(db).read(...)` execute immediately and return `list[dict]`.
  Empty reads return `[]`. Values of declared fields are normalized by the
  Model conversion rules (for example SQLite integer booleans become `bool`).
- `Model.join(models, *, cols=None, where=None, order_by=None, limit=None,
  offset=0, on=None)` returns `list[dict]`, with documented `left__` and
  `right__`/`right1__` result aliases. Model-qualified column references are
  translated to the underlying table names.
- `create(**values)` returns the created Model instance, including the generated
  primary key when supported. `update(values=..., where=...)` and
  `delete(where=...)` return integer driver-reported affected-row counts.
  Unfiltered updates/deletes require `confirm=True`.

```python
rows = Book.using(db).read(where={"published": True}, order_by={"id": "DESC"})
joined = Book.using(db).join(models=[Author], cols=["Book.title", "Author.name"])
```

## Direct parameterized SQL (restored Legacy meaning)

`Model.query(sql, parameters=None)` and `Model.using(db).query(sql, parameters=None)`
execute SQL immediately using the bound connection. Statements with result rows
return `list[dict]`; statements without result rows return the driver-reported
integer affected-row count. Use named `:parameter` placeholders for values.

```python
rows = Book.using(db).query(
    "SELECT * FROM books WHERE id = :id", {"id": 42}
)
```

Parameter binding prevents SQL values from being interpreted as SQL syntax.
SQL identifiers and raw statement text are developer-controlled: never build
them from untrusted input. The method does not add a transaction, filter, or
whole-table write confirmation; the caller owns those responsibilities.

## Legacy connection-level query and fetch methods

`Connection.query(sql, parameters=None)` restores the application-level
`database.query(...).fetchall()` pattern. It returns a lightweight, cursor-like
`QueryResult` with `fetchall()`, `fetchone()`, and `rowcount`. Rows are fetched
immediately and the underlying DB cursor is closed before returning; fetches
consume the in-memory result. This is **not** a streaming cursor. Calling
fetch methods on write-only statements raises `ValueError`.

```python
rows = db.query(
    "SELECT title FROM articles WHERE lang_id = :lang LIMIT :limit",
    {"lang": lang, "limit": limit},
).fetchall()
```

Unlike `Connection.query()`, `Model.query()` from the preceding raw-SQL batch
still returns an immediate `list[dict]` or affected-row integer. These two
return contracts are intentionally distinct for now; unifying them requires a
separate compatibility decision. SQL strings built with f-strings or `%`/`+`
interpolation of user-controlled values are unsafe; always bind values.

## Temporary Alpha 8 compatibility path

Previous lazy-query callers now use `.legacy_query(...)`, for example
`.legacy_query().all()` and `.legacy_query().join(...)`. This is a temporary
migration/testing interface, not the intended permanent public API. It must
not be silently removed before the Alpha 8 → 9 approval gate.

The temporary `as_dicts=True` argument is removed. Direct `read()` returns
plain dictionaries by default.

## Field type declaration status

`column(str/int/float/bool/date/datetime, ...)` is currently supported.
SQL-specific declarations such as `VARCHAR(255)` and `DECIMAL(12,2)` are
**not yet accepted** by `column()`. A dialect-aware, validated SQL type option
requires a separate schema/snapshot/migration compatibility review; this
batch intentionally does not silently change that contract.

## Remaining gates

- Live MySQL and PostgreSQL regression must pass in disposable databases.
- NoteFlow must be verified against this version before default replacement.
- Driver-specific `UPDATE` row-count semantics (matched versus changed rows),
  and less-common date/decimal/binary types, still need a documented policy.
- No automatic ORM relationship discovery or SQLAlchemy-like fluent public
  interface is approved. The SQLAlchemy-backed default remains unchanged.
- No Alpha 9 backend replacement without explicit approval.
