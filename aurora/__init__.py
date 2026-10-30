from aurora.core.application import Application

# Public, concise name; Application remains available for compatibility.
Aurora = Application
from aurora.core.controller import Controller
from aurora.core.response import redirect
from aurora.view import View, view
from aurora.urls import url_for
from aurora.validation import validate

__version__ = "1.0.0a5"

__all__ = [
    "Aurora",
    "Application",
    "Controller",
    "View",
    "view",
    "redirect",
    "url_for",
    "validate",
]