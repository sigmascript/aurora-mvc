"""Internal validation helpers for Application constructor options.

These functions are intentionally private. Public configuration remains
keyword-based so existing Aurora applications do not need migration.
"""

import re
from pathlib import Path

from aurora.security.sessions import SessionSettings


def validate_session_settings(sessions, session_settings):
    """Resolve the supported session forms without changing their errors."""
    if isinstance(sessions, SessionSettings):
        if session_settings is not None:
            raise ValueError("Pass session settings only once")
        settings = sessions
    elif sessions is True:
        if not isinstance(session_settings, SessionSettings):
            raise ValueError("sessions=True requires SessionSettings(database_path=...)")
        settings = session_settings
    elif sessions is False:
        if session_settings is not None:
            raise ValueError("session_settings requires sessions=True")
        settings = None
    else:
        raise TypeError("sessions must be a boolean or SessionSettings")
    return settings


def validate_runtime_settings(
    environment, database_work, database_registry, max_request_body_bytes
):
    """Validate opt-in database integration and request body limits."""
    if environment not in {"development", "production"}:
        raise ValueError("environment must be development or production")
    # Database lifecycle is opt-in; existing applications remain unchanged.
    if (database_work is None) != (database_registry is None):
        raise ValueError("database_work and database_registry must be supplied together")
    if database_work is not None:
        from aurora.database.async_routed import RoutedWorkManager
        from aurora.database.database_registry import DatabaseRegistry

        if not isinstance(database_work, RoutedWorkManager):
            raise TypeError("database_work must be a RoutedWorkManager")
        if not isinstance(database_registry, DatabaseRegistry):
            raise TypeError("database_registry must be a DatabaseRegistry")
    if max_request_body_bytes is not None and (
        isinstance(max_request_body_bytes, bool)
        or not isinstance(max_request_body_bytes, int)
        or max_request_body_bytes < 0
    ):
        raise ValueError("max_request_body_bytes must be a non-negative integer or None")


def validate_locales(locales, default_locale):
    """Validate opt-in locale prefixes and the default locale."""
    if locales is None:
        if default_locale is not None:
            raise ValueError("default_locale requires locales")
        validated_locales = None
        validated_default_locale = None
    else:
        if (
            isinstance(locales, (str, bytes))
            or not isinstance(locales, (tuple, list))
            or not locales
        ):
            raise ValueError("locales must be a non-empty list or tuple")
        if any(
            not isinstance(code, str)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", code)
            for code in locales
        ):
            raise ValueError("Locale codes must be lowercase language codes, e.g. en or pt-br")
        if len(set(locales)) != len(locales):
            raise ValueError("Duplicate locale code")
        if not isinstance(default_locale, str) or default_locale not in locales:
            raise ValueError("default_locale must be one of locales")
        validated_locales = tuple(locales)
        validated_default_locale = default_locale
    return validated_locales, validated_default_locale


def validate_locale_cookie(locale_cookie_name, locales, settings):
    """Reject invalid or conflicting locale preference cookie names."""
    if locale_cookie_name is not None:
        if locales is None:
            raise ValueError("locale_cookie_name requires locales")
        if not isinstance(locale_cookie_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*", locale_cookie_name
        ):
            raise ValueError("Invalid locale preference cookie name")
        if locale_cookie_name.startswith(("__Host-", "__Secure-")):
            raise ValueError("Use an ordinary name for the optional locale preference cookie")
        if settings is not None and locale_cookie_name == settings.cookie_name:
            raise ValueError("Locale preference cookie cannot share the session cookie name")


def validate_static_settings(static_directory, statics_directory, static_url):
    """Normalize the legacy and current static-directory options."""
    if (
        not isinstance(static_url, str)
        or not static_url.startswith("/")
        or static_url == "/"
        or static_url.endswith("/")
    ):
        raise ValueError("static_url must be an absolute URL prefix such as /statics")
    if static_directory is not None and statics_directory is not None:
        raise ValueError("Use either statics_directory or static_directory, not both")
    directory = statics_directory if statics_directory is not None else static_directory
    return static_url, Path(directory).resolve() if directory is not None else None
