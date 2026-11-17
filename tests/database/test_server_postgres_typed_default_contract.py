"""PostgreSQL literal-default catalog evidence and coherent forward-review contract."""

from types import SimpleNamespace

import pytest

from aurora.database.models import Model, column
from aurora.database.planning import SchemaChange, SchemaPlan
from aurora.database import server_model_review as review
from aurora.database.server_initial_verification import (
    postgres_literal_value, server_default_matches,
)


@pytest.mark.parametrize('kind,default,raw,equal', [
    (int, 7, '7::bigint', True),
    (int, 7, '7::integer', True),
    (int, 7, '8::bigint', False),
    (float, 2.5, '2.5::double precision', True),
    (float, 2.5, '2.6::double precision', False),
    (bool, True, 'true::boolean', True),
    (bool, False, 'false::boolean', True),
    (str, 'hello', "'hello'::text", True),
    (str, "it's", "'it''s'::character varying", True),
    (str, 'hello', "'goodbye'::text", False),
])
def test_known_postgresql_catalog_literal_is_compared_by_value(kind, default, raw, equal):
    field = column(kind, default=default)
    assert server_default_matches(field, raw, 'postgresql') is equal


@pytest.mark.parametrize('kind,raw', [
    (int, '1 + 6'),
    (int, 'nextval(\'seq\'::regclass)'),
    (int, '7::text'),
    (int, '7::bigint::integer'),
    (int, '7.5::bigint'),
    (float, 'now()'),
    (float, '2.5::text'),
    (bool, 'true OR false'),
    (bool, 'true::text'),
    (str, 'current_user'),
    (str, "'hello'::regclass"),
    (str, "'hello' || 'world'"),
])
def test_unsafe_or_unrelated_default_expressions_are_rejected(kind, raw):
    field = column(kind, default=(True if kind is bool else 'hello' if kind is str else 7))
    assert postgres_literal_value(field, raw) is None
    assert not server_default_matches(field, raw, 'postgresql')
    assert not review._verified_prior_default(field, raw, 'postgresql')


def test_casted_postgresql_default_can_be_replaced_and_is_digest_bound(monkeypatch):
    state = {'default': '7::bigint'}

    class Records(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=9)

    monkeypatch.setattr(review, 'plan_schema', lambda _conn, model: SchemaPlan(
        model.__table_name__, True, (SchemaChange('default_mismatch', 'visits', ''),), ()))

    def physical(_conn, name):
        return SimpleNamespace(
            exists=True, uniqueness_verified=True, constraints_verified=True,
            indexes=(f'{name}_pkey',), unique_columns=(),
            columns=(SimpleNamespace(name='id', sql_type='BIGINT', nullable=False,
                                     primary_key=True, default_sql=None),
                     SimpleNamespace(name='visits', sql_type='BIGINT', nullable=False,
                                     primary_key=False, default_sql=state['default'])))

    monkeypatch.setattr(review, 'inspect_table', physical)
    db = SimpleNamespace(dialect='postgresql')
    before = review.review_server_model_changes(db, (Records,), 'default', 'identity')
    assert 'ALTER COLUMN "visits" SET DEFAULT 9' in before.statements[0]
    assert before.operations[0]['edits'][0]['before']['default'] == '7::bigint'
    state['default'] = '8::bigint'
    after = review.review_server_model_changes(db, (Records,), 'default', 'identity')
    assert before.review_sha256 != after.review_sha256


def test_mysql_still_refuses_postgresql_cast_syntax():
    field = column(int, default=7)
    assert not server_default_matches(field, '7::bigint', 'mysql')
    assert not review._verified_prior_default(field, '7::bigint', 'mysql')

@pytest.mark.parametrize('observed,expected_change', [
    ('7::bigint', False),
    ('8::bigint', True),
    ('7::text', True),
])
def test_real_schema_planner_uses_typed_catalog_evidence(monkeypatch, observed, expected_change):
    from aurora.database import planning

    class Records(Model):
        __table__ = 'records'
        id = column(int, primary_key=True)
        visits = column(int, default=7)

    def table(_conn, name):
        return SimpleNamespace(
            name=name, exists=True, indexes=(f'{name}_pkey',),
            uniqueness_verified=True, constraints_verified=True,
            foreign_keys=(), foreign_key_actions=(), unique_columns=(),
            columns=(SimpleNamespace(name='id', sql_type='BIGINT', nullable=False,
                                     primary_key=True, default_sql=None),
                     SimpleNamespace(name='visits', sql_type='BIGINT', nullable=False,
                                     primary_key=False, default_sql=observed)))

    monkeypatch.setattr(planning, 'inspect_table', table)
    differences = planning.plan_schema(SimpleNamespace(dialect='postgresql'), Records).changes
    assert ('default_mismatch' in [c.kind for c in differences]) is expected_change
