"""Strict, inert application defaults for explicit Aurora project configuration.

This file never evaluates Python or connects to services. Configured user
preferences belong to an application's data store, not this document.
"""

import json
import math
import os
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
import re
import stat
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_FORMAT = "aurora.application.v1"
_MAX_FILE_BYTES = 65536
_MAX_CONSTANTS = 64
_CONSTANT_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")


def _unique_pairs(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key in application configuration")

        result[key] = value

    return result


def _reject_non_json(_value):
    raise ValueError("Non-JSON numeric value in application configuration")


def _read_document(source):
    path = Path(source).expanduser()

    if path.is_symlink() or not path.is_file():
        raise ValueError("Application configuration must be a regular, non-symlink file")

    # On platforms that implement O_NOFOLLOW, protect against the path being
    # swapped to a symlink after the initial check. Reject special files too.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(path, flags)

        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())

            if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_FILE_BYTES:
                raise ValueError("Application configuration must be a regular file under 64 KiB")

            raw = stream.read(_MAX_FILE_BYTES + 1)

            if len(raw) > _MAX_FILE_BYTES:
                raise ValueError("Application configuration exceeds 64 KiB")

        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_non_json)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot read application configuration JSON") from error

    if not isinstance(document, dict) or document.get("format") != _FORMAT:
        raise ValueError("Unsupported application configuration format")

    if set(document) - {"format", "application", "limits", "constants"}:
        raise ValueError("Unknown application configuration section")

    return document


@dataclass(frozen=True)
class ApplicationSettings:
    """Immutable validated metadata and constructor options."""

    name: str
    timezone_name: str
    tzinfo: object
    constants: object
    constructor_options: object


def load_application_settings(source):
    """Read and validate a versioned application JSON without side effects."""
    document = _read_document(source)
    application = document.get("application", {})
    limits = document.get("limits", {})
    constants = document.get("constants", {})

    if not isinstance(application, dict) or not isinstance(limits, dict) or not isinstance(constants, dict):
        raise ValueError("Application, limits and constants must be JSON objects")

    if set(application) - {"name", "timezone", "locales", "default_locale", "locale_cookie_name"}:
        raise ValueError("Unknown application setting")

    if set(limits) - {"max_request_body_bytes"}:
        raise ValueError("Unknown application limit")

    name = application.get("name", "Aurora Application")

    if (not isinstance(name, str) or not name.strip() or len(name) > 128 or
            any(ord(character) < 32 for character in name)):
        raise ValueError("Application name must be a short, nonempty string")

    timezone_name = application.get("timezone", "UTC")

    if not isinstance(timezone_name, str) or len(timezone_name) > 100 or not timezone_name:
        raise ValueError("Application timezone must be a valid IANA time-zone name")

    if timezone_name == "UTC":
        tzinfo = timezone.utc
    else:
        try:
            tzinfo = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as error:
            raise ValueError(
                "Application timezone is unavailable; install time-zone data (tzdata on Windows)"
            ) from error

    constructor_options = {}

    # Reuse the Application constructor's existing locale validation contract.
    if "locales" in application or "default_locale" in application:
        from .configuration import validate_locales

        locales, default_locale = validate_locales(
            application.get("locales"), application.get("default_locale")
        )
        constructor_options.update(locales=locales, default_locale=default_locale)

    if "locale_cookie_name" in application:
        from .configuration import validate_locale_cookie

        cookie_name = application["locale_cookie_name"]
        validate_locale_cookie(cookie_name, constructor_options.get("locales"), None)
        constructor_options["locale_cookie_name"] = cookie_name

    if "max_request_body_bytes" in limits:
        value = limits["max_request_body_bytes"]

        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("max_request_body_bytes must be non-negative integer or null")

        constructor_options["max_request_body_bytes"] = value

    if len(constants) > _MAX_CONSTANTS:
        raise ValueError("Application configuration has too many constants")

    for key, value in constants.items():
        if not _CONSTANT_KEY.fullmatch(key):
            raise ValueError("Invalid application constant name")

        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError("Application constants must contain simple JSON scalar values")

        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Application constant must be finite")

        if isinstance(value, str) and len(value) > 4096:
            raise ValueError("Application constant string is too large")

    return ApplicationSettings(
        name=name.strip(), timezone_name=timezone_name, tzinfo=tzinfo,
        constants=MappingProxyType(dict(constants)),
        constructor_options=MappingProxyType(constructor_options),
    )
