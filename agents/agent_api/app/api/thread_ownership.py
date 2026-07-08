"""Compatibility shim for thread ownership middleware."""

from agents.agent_api.app.middleware.thread_ownership import validate_thread_ownership

__all__ = ["validate_thread_ownership"]
