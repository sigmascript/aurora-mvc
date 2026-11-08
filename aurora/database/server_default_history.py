"""Original CREATE and Initial proof for adding a literal SQL column default."""

from copy import copy
from hashlib import sha256

from .history import list_history, review_history
from .model_identity import model_identity_map
from .models import Model
from .schema import create_table_sql
from .server_initial_verification import verify_server_initial_schema
from .server_migration_timeline import inspect_server_timeline


def default_addition_predecessor_models(models, operation):
    """Construct old declarations without modifying the user's default fields."""
    if (not isinstance(operation, dict) or operation.get("kind") != "add_default"
            or set(operation) != {"kind", "table", "column"}):
        raise ValueError("A reviewed SQL default addition is required")

    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != operation["table"]:
            predecessor.append(model)
            continue

        name = operation["column"]

        if matched or name not in model.__columns__:
            raise ValueError("Historical default target is ambiguous")

        current = model.__columns__[name]

        if (current.primary_key or current.unique or current.references is not None
                or current.default is None or current.server_default is not None):
            raise ValueError("Historical default cannot change constrained columns")

        previous = copy(current)
        previous.default = None
        fields = {field: previous if field == name else declaration
                  for field, declaration in model.__columns__.items()}
        predecessor.append(type("HistoricalTable", (Model,),
                                {"__table__": model.__table_name__, **fields}))
        matched = True

    if not matched:
        raise ValueError("Historical default model is missing")

    return tuple(predecessor)


def verify_server_default_predecessor(connection, models, alias, proposal):
    """Require exact original table fingerprints and physical schema equality."""
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server default migration requires MySQL or PostgreSQL")

    if (len(proposal.operations) != 1
            or proposal.operations[0].get("kind") != "add_default"):
        raise ValueError("Exactly one reviewed default addition is required")

    timeline = inspect_server_timeline(connection, alias)

    if (timeline is None or len(timeline) != 1 or timeline[0].status != "applied"
            or timeline[0].model_tables != model_identity_map(models)):
        raise RuntimeError("Trusted Initial model mapping is required for default addition")

    previous = default_addition_predecessor_models(models, proposal.operations[0])
    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(previous):
        raise RuntimeError("Original per-table journal is unresolved")

    fingerprints = {
        model.__table_name__: sha256(create_table_sql(model, connection.dialect).encode()).hexdigest()
        for model in previous
    }

    if ({entry.table for entry in history} != set(fingerprints)
            or any(entry.status != "applied" or entry.dialect != connection.dialect
                   or entry.fingerprint != fingerprints[entry.table] for entry in history)):
        raise RuntimeError("Original CREATE fingerprints cannot prove default predecessor")

    try:
        digest = verify_server_initial_schema(connection, previous)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("Default predecessor differs from physical schema") from error

    if digest != timeline[0].schema_sha256:
        raise RuntimeError("Original Initial schema fingerprint cannot be verified")

    return previous
