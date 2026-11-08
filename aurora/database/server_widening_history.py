"""Read-only historical proof for one compatible server column widening."""

from hashlib import sha256

from .history import list_history, review_history
from .model_identity import model_identity_map
from .models import Model, column
from .schema import create_table_sql
from .server_initial_verification import verify_server_initial_schema
from .server_migration_timeline import inspect_server_timeline


def widening_predecessor_models(models, operation):
    """Reconstruct old types without mutating application model declarations."""
    if not isinstance(operation, dict) or operation.get("kind") != "widen_column":
        raise ValueError("A reviewed widening operation is required")

    table = operation["table"]
    name = operation["column"]
    predecessor = []
    matched = False

    for model in models:
        if model.__table_name__ != table:
            predecessor.append(model)
            continue

        if matched or name not in model.__columns__:
            raise ValueError("Historical widening target is ambiguous")

        current = model.__columns__[name]

        if (current.primary_key or current.unique or current.references is not None
                or current.default is not None or current.server_default is not None):
            raise ValueError("Historical widening cannot change constrained columns")

        previous_field = column(operation["from"], nullable=current.nullable)
        fields = {field: previous_field if field == name else declaration
                  for field, declaration in model.__columns__.items()}
        predecessor.append(type("HistoricalTable", (Model,),
                                {"__table__": table, **fields}))
        matched = True

    if not matched:
        raise ValueError("Historical widening model is missing")

    return tuple(predecessor)


def verify_server_widening_predecessor(connection, models, alias, proposal):
    """Require native Initial, CREATE fingerprints and exact pre-DDL catalog."""
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server widening requires MySQL or PostgreSQL")

    if len(proposal.operations) != 1 or proposal.operations[0]["kind"] != "widen_column":
        raise ValueError("Exactly one reviewed widening is required")

    timeline = inspect_server_timeline(connection, alias)

    if (timeline is None or len(timeline) != 1 or timeline[0].status != "applied"
            or timeline[0].model_tables != model_identity_map(models)):
        raise RuntimeError("Trusted Initial model mapping is required for widening")

    predecessor = widening_predecessor_models(models, proposal.operations[0])
    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(predecessor):
        raise RuntimeError("Original per-table journal is unresolved")

    fingerprints = {
        model.__table_name__: sha256(create_table_sql(model, connection.dialect).encode()).hexdigest()
        for model in predecessor
    }

    if ({entry.table for entry in history} != set(fingerprints)
            or any(entry.status != "applied" or entry.dialect != connection.dialect
                   or entry.fingerprint != fingerprints[entry.table] for entry in history)):
        raise RuntimeError("Original CREATE fingerprints cannot prove widening predecessor")

    try:
        digest = verify_server_initial_schema(connection, predecessor)
    except (RuntimeError, ValueError) as error:
        raise RuntimeError("Widening predecessor differs from physical schema") from error

    if digest != timeline[0].schema_sha256:
        raise RuntimeError("Original Initial schema fingerprint cannot be verified")

    return predecessor
