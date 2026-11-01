"""Small, validated SQL type declarations; never accept arbitrary DDL fragments."""

import re
from decimal import Decimal

_PATTERN = re.compile(r"^(TEXT|VARCHAR\(([1-9][0-9]{0,4})\)|DECIMAL\(([1-9][0-9]?),([0-9]{1,2})\))$", re.I)


def parse_sql_type(declaration):
    if not isinstance(declaration, str):
        raise TypeError("SQL type declaration must be a string")
    match = _PATTERN.fullmatch(declaration.strip())
    if match is None:
        raise ValueError("Unsupported SQL type; use TEXT, VARCHAR(n), or DECIMAL(p,s)")
    if match.group(1).upper() == "TEXT":
        return "TEXT", str
    if match.group(2) is not None:
        size = int(match.group(2))
        if size > 65535:
            raise ValueError("VARCHAR length exceeds supported limit")
        return f"VARCHAR({size})", str
    precision, scale = int(match.group(3)), int(match.group(4))
    if precision > 65 or scale > precision:
        raise ValueError("DECIMAL requires 1 <= precision <= 65 and 0 <= scale <= precision")
    return f"DECIMAL({precision},{scale})", Decimal
