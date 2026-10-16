"""Small, explicit validator for ordinary string form fields.

No automatic coercion or HTML form abstraction. Application-specific rules
(such as username policies and password confirmation) stay in the application.
"""

import re
from collections.abc import Mapping

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


def validate(data: Mapping[str, str], rules: Mapping[str, str]) -> dict[str, list[str]]:
    """Return field -> list of messages; an empty dict means valid.

    Rules are pipe-separated: required, email, min:N, max:N.
    Lengths count Python Unicode characters. Optional empty fields skip
    non-required rules. Unknown/malformed rules raise ValueError.
    """
    compiled = {}
    for field, specification in rules.items():
        if not isinstance(field, str) or not isinstance(specification, str):
            raise ValueError("Field names and rule specifications must be strings")
        tokens = specification.split("|")
        if not tokens or any(not token for token in tokens):
            raise ValueError(f"Invalid validation rules for {field!r}")
        checks = []
        for token in tokens:
            if token in ("required", "email"):
                checks.append((token, None))
            elif token.startswith(("min:", "max:")):
                name, _, value = token.partition(":")
                if not value.isascii() or not value.isdecimal():
                    raise ValueError(f"Invalid validation rule: {token}")
                checks.append((name, int(value)))
            else:
                raise ValueError(f"Unknown validation rule: {token}")
        compiled[field] = checks

    errors: dict[str, list[str]] = {}
    for field, checks in compiled.items():
        value = data.get(field, "")
        if not isinstance(value, str):
            errors[field] = ["Must be text."]
            continue
        if not value.strip():
            if any(name == "required" for name, _ in checks):
                errors[field] = ["This field is required."]
            continue
        messages = []
        for name, limit in checks:
            if name == "email" and (len(value) > 254 or not _EMAIL.fullmatch(value)):
                messages.append("Enter a valid email address.")
            elif name == "min" and len(value) < limit:
                messages.append(f"Must be at least {limit} characters.")
            elif name == "max" and len(value) > limit:
                messages.append(f"Must be at most {limit} characters.")
        if messages:
            errors[field] = messages
    return errors
