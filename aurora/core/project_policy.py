"""Resolve project-local operational production safeguards.

Application diagnostic configuration and CLI restrictions share the same
validated project file. Environment variables never override this policy.
"""

from pathlib import Path

from .app_settings import load_application_settings


def project_is_production(project_root=None):
    """Fail closed when project settings are missing or cannot be validated."""
    root = Path(project_root) if project_root is not None else Path.cwd()
    settings_file = root / "aurora_config.json"

    if not settings_file.is_file():
        raise PermissionError(
            "Project production policy unavailable: aurora_config.json is missing. "
            "Select a configured project before modifying it."
        )

    try:
        settings = load_application_settings(settings_file)
    except (OSError, ValueError) as error:
        raise PermissionError("Project production policy is invalid; operation refused") from error

    return settings.constructor_options.get("production", True)
