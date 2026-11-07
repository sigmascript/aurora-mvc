"""Model-driven SQLite drift detection is precise, read-only, and non-executable."""

from aurora.database import Model, column, connect, create_table, plan_schema


class Invoice(Model):
    __table__ = "drift_invoices"
    id = column(int, primary_key=True)
    description = column(str, length=40)
    amount = column("DECIMAL(9,2)")
    status = column(str, default="draft")
    note = column(str, nullable=True)


def test_matching_model_does_not_report_constant_default_or_sql_type_drift():
    with connect("sqlite:///:memory:") as db:
        create_table(db, Invoice)
        plan = plan_schema(db, Invoice)
        assert not plan.changes
        assert plan.execution_allowed is False


def test_type_length_precision_nullability_and_defaults_are_distinct_changes():
    class ChangedInvoice(Model):
        __table__ = "drift_invoices"
        id = column(int, primary_key=True)
        description = column(str, length=80)
        amount = column("DECIMAL(10,3)")
        status = column(str, default="ready")
        note = column(str)

    with connect("sqlite:///:memory:") as db:
        create_table(db, Invoice)
        before = db.fetch_first("SELECT sql FROM sqlite_master WHERE name='drift_invoices'")
        plan = plan_schema(db, ChangedInvoice)
        observed = {(change.kind, change.column) for change in plan.changes}

        assert observed == {
            ("type_mismatch", "description"),
            ("type_mismatch", "amount"),
            ("default_mismatch", "status"),
            ("nullability_mismatch", "note"),
        }
        assert plan.execution_allowed is False
        assert db.fetch_first("SELECT sql FROM sqlite_master WHERE name='drift_invoices'") == before


def test_missing_and_unexpected_defaults_are_reported_without_execution():
    class NoStatusDefault(Model):
        __table__ = "drift_invoices"
        id = column(int, primary_key=True)
        description = column(str, length=40)
        amount = column("DECIMAL(9,2)")
        status = column(str)
        note = column(str, nullable=True)

    with connect("sqlite:///:memory:") as db:
        create_table(db, Invoice)
        plan = plan_schema(db, NoStatusDefault)
        assert ("default_mismatch", "status") in {
            (change.kind, change.column) for change in plan.changes
        }


def test_foreign_key_target_change_is_detected_but_never_executed():
    class Customer(Model):
        id = column(int, primary_key=True)

    class Order(Model):
        id = column(int, primary_key=True)
        customer_id = column(int, references="customer.id")

    class ChangedOrder(Model):
        __table__ = "order"
        id = column(int, primary_key=True)
        customer_id = column(int)

    with connect("sqlite:///:memory:") as db:
        create_table(db, Customer)
        create_table(db, Order)
        plan = plan_schema(db, ChangedOrder)
        assert ("foreign_key_mismatch", "customer_id") in {
            (change.kind, change.column) for change in plan.changes
        }
        assert plan.execution_allowed is False
