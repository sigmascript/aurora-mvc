"""Validated ordering and parameter-bound filter compilation for AuroraSQL models.

Keep SQL operator selection and filter nesting limits together so the model
query layer can focus on execution and result construction.
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
    "gt": ">", "greater-than": ">", "ge": ">=", "greater-equal": ">=",
    "lt": "<", "less-than": "<", "le": "<=", "less-equal": "<=",
    "like": "LIKE", "l": "LIKE", "not-like": "NOT LIKE", "nl": "NOT LIKE",
    "in": "IN", "i": "IN", "not-in": "NOT IN", "ni": "NOT IN",
    "between": "BETWEEN", "b": "BETWEEN", "not-between": "NOT BETWEEN", "nb": "NOT BETWEEN",
    "ne": "!=", "not-equal": "!=",
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
