"""Validate optional human-readable migration titles independently of identifiers."""

import sys


def migration_title(explicit, default):
    """Choose a bounded title without blocking scripts or noninteractive shells."""
    if explicit is None:
        explicit = input(f"Migration title [{default}]: ") if sys.stdin.isatty() else ""

    if not isinstance(explicit, str):
        raise ValueError("Migration title must be text")

    if any(ord(char) < 32 or ord(char) == 127 for char in explicit):
        raise ValueError("Migration title contains control characters")

    title = explicit.strip() or default

    if (len(title) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise ValueError("Migration title is too long or contains control characters")

    return title
