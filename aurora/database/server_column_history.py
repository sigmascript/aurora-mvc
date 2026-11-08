"""Read-only historical proof for one ordinary server column addition.

An added model field changes the current declaration. Recover the predecessor
only when the Initial CREATE fingerprint and physical schema independently
agree. This module does not execute ALTER TABLE or update any journal.
"""

from hashlib import sha256

from .history import list_history, review_history
from .model_identity import model_identity_map
from .models import Model
from .schema import create_table_sql
from .server_initial_verification import verify_server_initial_schema
from .server_migration_timeline import inspect_server_timeline


def column_addition_predecessors(models, operation):
    """Reconstruct old table declarations without modifying user model classes."""
    if not isinstance(operation, dict) or operation.get("kind") != "add_column":
        raise ValueError("One explicit column-addition operation is required")

    target = operation["table"]
    field_name = operation["column"]
    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != target:
            predecessor.append(model)
            continue

        if matched or field_name not in model.__columns__:
            raise RuntimeError("Column addition target is ambiguous")

        remaining = {name: field for name, field in model.__columns__.items()
                     if name != field_name}

        if not remaining or not any(field.primary_key for field in remaining.values()):
            raise ValueError("Existing primary-key structure must remain unchanged")

        # The ephemeral class is never imported, registered or installed on a
        # user model. It exists only to regenerate the old CREATE fingerprint.
        historic = type("HistoricalTable", (Model,), {"__table__": target, **remaining})
        predecessor.append(historic)
        matched = True

    if not matched:
        raise ValueError("Reviewed added column does not belong to a selected model")

    return tuple(predecessor)


def verify_server_column_addition_predecessor(connection, models, alias, proposal):
    """Prove a single added nullable field against the original Initial state.

    Return predecessor declarations solely for transaction-local review.
    Never infer a rename, backfill, key change or destructive DDL.
    """
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server column history requires MySQL or PostgreSQL")

    if (len(proposal.operations) != 1 or proposal.operations[0]["kind"] != "add_column"
            or len(proposal.statements) != 1):
        raise ValueError("One reviewed ordinary column addition is required")

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) != 1 or timeline[0].status != "applied":
        raise RuntimeError("A verified native Initial is required for column addition")

    if timeline[0].model_tables != model_identity_map(models):
        raise RuntimeError("Historical model-table identities differ")

    predecessor = column_addition_predecessors(models, proposal.operations[0])

    journal = list_history(connection)

    if review_history(connection, entries=journal) or len(journal) != len(predecessor):
        raise RuntimeError("Historical per-table operation journal is unresolved")

    expected = {model.__table_name__: sha256(create_table_sql(
        model, connection.dialect).encode("utf-8")).hexdigest() for model in predecessor}

    if ({entry.table for entry in journal} != set(expected)
            or any(entry.status != "applied" or entry.dialect != connection.dialect
                   or entry.fingerprint != expected[entry.table] for entry in journal)):
        raise RuntimeError("Original CREATE fingerprint differs from model predecessor")

    try:
        actual = verify_server_initial_schema(connection, predecessor)
    except (ValueError, RuntimeError) as error:
        raise RuntimeError("Physical predecessor does not match approved Initial") from error

    if actual != timeline[0].schema_sha256:
        raise RuntimeError("Historical Initial schema fingerprint differs")

    return tuple(predecessor)
