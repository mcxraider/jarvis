"""Compatibility shim for the fresh-thread quota middleware."""

from agents.agent_api.app.middleware.rate_limit import (
    QuotaResult,
    consume_new_thread_quota,
)

check_rate_limit = consume_new_thread_quota

__all__ = ["QuotaResult", "check_rate_limit", "consume_new_thread_quota"]
