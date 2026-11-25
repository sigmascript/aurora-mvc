from aurora.core.application import Application

from aurora.core.controller import Controller
from aurora.core.response import JSONResponse, redirect
from aurora.urls import url_for, with_query
from aurora.validation import validate
from aurora.view import View, view

# Public, concise name; Application remains available for compatibility.
Aurora = Application

__version__ = "1.0.0a8"

__all__ = [
    "Aurora",
    "Application",
    "Controller",
    "View",
    "view",
    "redirect",
    "JSONResponse",
    "url_for",
    "with_query",
    "validate",
]
