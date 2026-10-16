"""Request-local session data and lifecycle intent.

Persistence, token rotation and cookie updates are responsibilities of the
session manager (Correction #2.2), not this data object.
"""

import json
import secrets
from collections.abc import MutableMapping


class Session(MutableMapping):
    """Dictionary-like request session with tracked changes.

    Constructed by Aurora's future session manager, not by application code.
    The lifecycle methods record intent; a manager must commit that intent
    before an HTTP response is sent.
    """

    def __init__(self, data=None):
        self._data = {}
        self._changed = False
        self._rotation_requested = False
        self._invalidated = False
        for key, value in (data or {}).items():
            self._validate(key, value)
            self._data[key] = value

    @staticmethod
    def _validate(key, value):
        if not isinstance(key, str):
            raise TypeError("Session keys must be strings")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TypeError("Session values must be JSON-serializable") from exc

    def _ensure_active(self):
        if self._invalidated:
            raise RuntimeError("This session has been invalidated")

    def __getitem__(self, key):
        self._ensure_active()
        return self._data[key]

    def __setitem__(self, key, value):
        self._ensure_active()
        self._validate(key, value)
        self._data[key] = value
        self._changed = True

    def __delitem__(self, key):
        self._ensure_active()
        del self._data[key]
        self._changed = True

    def __iter__(self):
        self._ensure_active()
        return iter(self._data)

    def __len__(self):
        self._ensure_active()
        return len(self._data)

    @property
    def changed(self):
        """Whether stored session values have been modified."""
        return self._changed

    @property
    def rotation_requested(self):
        """Whether the session identifier must be rotated on commit."""
        return self._rotation_requested

    @property
    def invalidated(self):
        """Whether the session must be revoked on commit."""
        return self._invalidated

    async def regenerate(self):
        """Request identifier rotation, preserving current session values.

        The session manager performs the actual secure rotation at commit.
        """
        self._ensure_active()
        # CSRF tokens must not survive an authentication boundary.
        self._data.pop("_aurora_csrf_token", None)
        self._changed = True
        self._rotation_requested = True

    async def invalidate(self):
        """Request revocation and prevent further use of this session."""
        self._data.clear()
        self._invalidated = True
        self._changed = True
        self._rotation_requested = False
