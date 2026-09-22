"""Session Service — unified session management API.

Provides REST endpoints for creating, querying, and managing agent sessions
backed by SessionStore.  This service replaces session_proxy in Wave 6.
"""

from .store import SessionStoreWrapper, get_store

__all__ = [
    "SessionStoreWrapper",
    "get_store",
]
