"""Validated SQL filters, ordering, and direct JOIN queries for AuroraSQL models.

Keep query validation and compilation independent of public model declarations.
"""


def _normalize_ordering(order_by, columns):
    """Normalize safe, ordered column specifications into SQL directions."""

    if order_by is None:
        return None

    if isinstance(order_by, str):
        items = [order_by]
    elif isinstance(order_by, (list, tuple)):
        items = order_by
    elif isinstance(order_by, dict):
        items = list(order_by.items())
    else:
        raise ValueError("order_by must be a column, list, or direction dictionary")

    if not items:
        raise ValueError("order_by cannot be empty")

    ordering = []
    seen = set()

    for item in items:
        if isinstance(order_by, dict):
            name, direction = item

            if not isinstance(direction, str) or direction.upper() not in ("ASC", "DESC"):
                raise ValueError("Ordering direction must be ASC or DESC")

            descending = direction.upper() == "DESC"
        else:
            if not isinstance(item, str) or not item:
                raise ValueError("Ordering columns must be nonempty strings")

            descending = item.startswith("-")
            name = item[1:] if descending else item

        if name not in columns:
            raise ValueError(f"Unknown ordering column: {name!r}")

        if name in seen:
            raise ValueError(f"Duplicate ordering column: {name!r}")

        seen.add(name)
        ordering.append((name, descending))

    return tuple(ordering)


# Keep the useful Legacy suffixes without accepting arbitrary SQL expressions.
_FILTER_OPERATORS = {
    "gt": ">",
    "greater-than": ">",
    "ge": ">=",
    "greater-equal": ">=",
    "lt": "<",
    "less-than": "<",
    "le": "<=",
    "less-equal": "<=",
    "like": "LIKE",
    "l": "LIKE",
    "not-like": "NOT LIKE",
    "nl": "NOT LIKE",
    "in": "IN",
    "i": "IN",
    "not-in": "NOT IN",
    "ni": "NOT IN",
    "between": "BETWEEN",
    "b": "BETWEEN",
    "not-between": "NOT BETWEEN",
    "nb": "NOT BETWEEN",
    "ne": "!=",
    "not-equal": "!=",
}


def _compile_filters(filters, columns, quote, *, prefix="", identifiers=None):
    """Compile bounded AND/OR groups with validated fields and bound values.

    Plain dictionary entries are implicitly ANDed. Explicit $and/$or groups
    accept nonempty lists of nonempty dictionaries. A depth limit avoids
    unbounded recursive filter structures.
    """

    if filters is None:
        filters = {}

    if not isinstance(filters, dict):
        raise TypeError("where must be a dictionary")

    parameters = {}
    next_parameter = 0
    max_depth = 8

    def bind(value):
        nonlocal next_parameter
        key = f"w{next_parameter}"
        next_parameter += 1
        parameters[key] = value
        return f":{key}"

    def compile_group(group, depth):
        if depth > max_depth:
            raise ValueError("Filter nesting exceeds maximum depth")

        if not isinstance(group, dict):
            raise TypeError("Filter groups must be dictionaries")

        if not group:
            raise ValueError("Filter groups cannot be empty")

        clauses = []

        for raw_name, value in group.items():
            if not isinstance(raw_name, str):
                raise TypeError("Filter column must be a string")

            if raw_name in ("$and", "$or"):
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{raw_name} requires a nonempty list of dictionaries")

                children = [compile_group(child, depth + 1) for child in value]
                joiner = " AND " if raw_name == "$and" else " OR "
                clauses.append("(" + joiner.join(children) + ")")
                continue

            name, separator, suffix = raw_name.partition("--")
            operator = _FILTER_OPERATORS.get(suffix) if separator else "="

            if name not in columns:
                raise TypeError(f"Unknown column: {name}")

            if operator is None:
                raise ValueError(f"Unsupported filter operator: {suffix}")

            field = columns[name]
            identifier = identifiers[name] if identifiers is not None else prefix + quote(name)

            if operator in ("IN", "NOT IN"):
                if not isinstance(value, (list, tuple, set, frozenset)) or not value:
                    raise ValueError("IN filters require a nonempty collection")

                if isinstance(value, (set, frozenset)):
                    raise TypeError("IN filters require an ordered list or tuple")

                parts = []

                for item in value:
                    if item is None:
                        raise ValueError("IN filters cannot contain None")

                    field.validate(item)
                    parts.append(bind(item))

                clauses.append(f"{identifier} {operator} ({', '.join(parts)})")
            elif operator in ("BETWEEN", "NOT BETWEEN"):
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise ValueError("BETWEEN filters require exactly two values")

                bounds = []

                for item in value:
                    if item is None:
                        raise ValueError("BETWEEN bounds cannot be None")

                    field.validate(item)
                    bounds.append(bind(item))

                clauses.append(f"{identifier} {operator} {bounds[0]} AND {bounds[1]}")
            elif value is None:
                if operator not in ("=", "!="):
                    raise ValueError("Only equality and inequality support None")

                field.validate(value)
                clauses.append(f"{identifier} IS {'NOT ' if operator == '!=' else ''}NULL")
            else:
                field.validate(value)

                if operator in ("LIKE", "NOT LIKE") and field.python_type is not str:
                    raise TypeError("LIKE requires a string column")

                clauses.append(f"{identifier} {operator} {bind(value)}")

        return " AND ".join(clauses)

    if not filters:
        return "", parameters

    return " WHERE " + compile_group(filters, 0), parameters


def execute_model_join(
    operations, models, *, cols=None, where=None, order_by=None, limit=None, offset=0, on=None
):
    """Execute a direct JOIN with model-aware column names and safe filters."""

    # Keep public model classes in models.py to preserve their import identities.
    from .models import Model, ModelResult, Query

    if isinstance(models, type):
        models = [models]

    if not isinstance(models, (list, tuple)) or not models:
        raise ValueError("models must be a nonempty list of Model classes")

    if any(
        not isinstance(model, type) or not issubclass(model, Model) or model is Model
        for model in models
    ):
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

        if "." not in name:
            return name

        if name.count(".") != 1:
            raise ValueError(f"Invalid qualified column: {name!r}")

        prefix, field = name.split(".")
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
            if key in ("$and", "$or"):
                if not isinstance(value, list):
                    raise TypeError("Filter groups must contain lists")

                result[key] = [translate_filters(item) for item in value]
            else:
                if not isinstance(key, str):
                    raise TypeError("Filter columns must be strings")

                name, separator, suffix = key.partition("--")
                result[translate(name) + (separator + suffix if separator else "")] = value

        return result

    if on is None:
        explicit = [None] * len(models)
    elif (
        len(models) == 1
        and isinstance(on, tuple)
        and len(on) == 2
        and all(isinstance(value, str) for value in on)
    ):
        explicit = [on]
    elif isinstance(on, (list, tuple)) and len(on) == len(models):
        explicit = list(on)
    else:
        raise ValueError("on must provide one (left, right) pair per joined model")

    query = Query(operations)
    available = [operations.model]

    for model, pair in zip(models, explicit):
        if pair is None:
            candidates = []

            for previous in available:
                for field, column_def in previous.__columns__.items():
                    if column_def.references:
                        table, referenced = column_def.references.split(".")

                        if table == model.__table_name__ and referenced in model.__columns__:
                            candidates.append((f"{previous.__table_name__}.{field}", referenced))

                for field, column_def in model.__columns__.items():
                    if column_def.references:
                        table, referenced = column_def.references.split(".")

                        if table == previous.__table_name__ and referenced in previous.__columns__:
                            candidates.append((f"{previous.__table_name__}.{referenced}", field))

            if len(candidates) != 1:
                raise ValueError(
                    f"Join to {model.__name__} requires an explicit on pair: "
                    f"found {len(candidates)} declared relationships"
                )

            left, right = candidates[0]
        else:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise TypeError("Each on entry must be a pair of column names")

            left, right = pair
            left, right = translate(left), translate(right)

            if "." in right:
                table, right = right.split(".")

                if table != model.__table_name__:
                    raise ValueError("Right join column must belong to the new model")

        if "." in left:
            base_table, base_field = left.split(".")

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
            if isinstance(order_by, dict)
            else order_by,
            query._available_columns(),
        )
        query = Query(
            operations,
            query.where,
            ordering=ordering,
            limit=query._limit,
            offset=query._offset,
            join=query._join,
            selected=query._selected,
        )

    if limit is not None:
        query = query.limit(limit)

    if offset:
        query = query.offset(offset)

    rows = query.all()
    columns = {}
    for _, model, field, key in query._join_projection():
        for prefix in (model.__name__, model.__table_name__):
            columns[f"{prefix}.{field}"] = key

    return ModelResult(rows, columns=columns)
