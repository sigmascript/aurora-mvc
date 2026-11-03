"""Internal direct JOIN execution for AuroraSQL model operations.

Kept separate from models.py to isolate relationship resolution and projection.
"""

from ._model_filters import _normalize_ordering


def execute_model_join(operations, models, *, cols=None, where=None, order_by=None,
                       limit=None, offset=0, on=None):
    """Execute a direct JOIN with model-aware column names and safe filters."""

    # Keep public model classes in models.py to preserve their import identities.
    from .models import Model, ModelResult, Query

    if isinstance(models, type):
        models = [models]

    if not isinstance(models, (list, tuple)) or not models:
        raise ValueError("models must be a nonempty list of Model classes")

    if any(not isinstance(model, type) or not issubclass(model, Model)
           or model is Model for model in models):
        raise TypeError("models must contain declared Model classes")

    all_models = [operations.model, *models]
    names = {}

    for model in all_models:
        for name in (model.__name__, model.__table_name__):
            if name in names and names[name] is not model:
                raise ValueError(f"Ambiguous model/table identifier: {name}")

            names[name] = model

    def translate(name):
        if not isinstance(name, str):
            raise TypeError("Column references must be strings")

        if '.' not in name:
            return name

        if name.count('.') != 1:
            raise ValueError(f"Invalid qualified column: {name!r}")

        prefix, field = name.split('.')
        model = names.get(prefix)

        if model is None or field not in model.__columns__:
            raise ValueError(f"Unknown qualified column: {name!r}")

        return f"{model.__table_name__}.{field}"

    def translate_filters(group):
        if group is None:
            return None

        if not isinstance(group, dict):
            raise TypeError("where must be a dictionary")

        result = {}

        for key, value in group.items():
            if key in ('$and', '$or'):
                if not isinstance(value, list):
                    raise TypeError("Filter groups must contain lists")

                result[key] = [translate_filters(item) for item in value]
            else:
                if not isinstance(key, str):
                    raise TypeError("Filter columns must be strings")

                name, separator, suffix = key.partition('--')
                result[translate(name) + (separator + suffix if separator else '')] = value

        return result

    if on is None:
        explicit = [None] * len(models)
    elif len(models) == 1 and isinstance(on, tuple) and len(on) == 2 and all(
            isinstance(value, str) for value in on):
        explicit = [on]
    elif isinstance(on, (list, tuple)) and len(on) == len(models):
        explicit = list(on)
    else:
        raise ValueError("on must provide one (left, right) pair per joined model")

    query = operations.legacy_query()
    available = [operations.model]

    for model, pair in zip(models, explicit):
        if pair is None:
            candidates = []

            for previous in available:
                for field, column_def in previous.__columns__.items():
                    if column_def.references:
                        table, referenced = column_def.references.split('.')

                        if table == model.__table_name__ and referenced in model.__columns__:
                            candidates.append((f"{previous.__table_name__}.{field}", referenced))

                for field, column_def in model.__columns__.items():
                    if column_def.references:
                        table, referenced = column_def.references.split('.')

                        if table == previous.__table_name__ and referenced in previous.__columns__:
                            candidates.append((f"{previous.__table_name__}.{referenced}", field))

            if len(candidates) != 1:
                raise ValueError(f"Join to {model.__name__} requires an explicit on pair: "
                                 f"found {len(candidates)} declared relationships")

            left, right = candidates[0]
        else:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise TypeError("Each on entry must be a pair of column names")

            left, right = pair
            left, right = translate(left), translate(right)

            if '.' in right:
                table, right = right.split('.')

                if table != model.__table_name__:
                    raise ValueError("Right join column must belong to the new model")

        if '.' in left:
            base_table, base_field = left.split('.')

            if base_table == operations.model.__table_name__:
                left = base_field

        query = query.join(model, on=(left, right))
        available.append(model)

    if cols is not None:
        if not isinstance(cols, (list, tuple)) or not cols:
            raise ValueError("cols must be a nonempty list of column names")

        query = query.select(*(translate(name) for name in cols))

    if where is not None:
        query = query.where_joined(translate_filters(where))

    if order_by is not None:
        ordering = _normalize_ordering(
            {translate(name): direction for name, direction in order_by.items()}
            if isinstance(order_by, dict) else order_by,
            query._available_columns())
        query = Query(operations, query.where, ordering=ordering, limit=query._limit,
                      offset=query._offset, join=query._join, selected=query._selected)

    if limit is not None:
        query = query.limit(limit)

    if offset:
        query = query.offset(offset)

    rows = query.all()
    columns = {}
    joined_models = [operations.model, *models]

    for index, model in enumerate(joined_models):
        alias = "left" if index == 0 else (
            "right" if len(joined_models) == 2 else f"right{index}")

        for field in model.__columns__:
            canonical = f"{model.__table_name__}.{field}"

            if query._selected is not None and canonical not in query._selected:
                continue

            key = f"{alias}__{field}"

            for prefix in (model.__name__, model.__table_name__):
                columns[f"{prefix}.{field}"] = key

    return ModelResult(rows, columns=columns)
