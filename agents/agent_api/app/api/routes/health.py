"""Health check routes.

``/health`` is a bare liveness signal (process is up). ``/health/detail`` is a
deeper dependency probe that backs the Telegram ``/status`` card: it verifies the
DeepSeek API and the requesting user's Todoist token are actually reachable.
Keep the two separate so a degraded dependency never makes liveness look dead.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import APIRouter

from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = logging.getLogger(__name__)

router = APIRouter()

# Each probe is self-bounded by its own network timeout; this is only a backstop
# in case a probe hangs before its request even starts.
_PROBE_TIMEOUT_SECONDS = 8.0
# Short per-request timeouts keep /status snappy — a health probe should fail fast
# rather than inherit the agent's generous request budget.
_DEEPSEEK_TIMEOUT_SECONDS = 5.0
_TODOIST_TIMEOUT_SECONDS = 5.0


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _error_detail(exc: Exception) -> str:
    """Privacy-safe, human-readable one-liner for a failed probe."""
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _check_deepseek() -> Dict[str, Any]:
    """Cheap DeepSeek reachability probe via the OpenAI-compatible models list.

    ``models.list()`` validates the endpoint + API key without spending any
    completion tokens. Never raises — a health probe must always resolve.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"ok": False, "detail": "no API key"}
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=_DEEPSEEK_TIMEOUT_SECONDS,
        )
        client.models.list()
        return {"ok": True, "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001 - health probe must never propagate
        return {"ok": False, "detail": _error_detail(exc)}


def _check_todoist(telegram_user_id: Optional[int]) -> Dict[str, Any]:
    """Per-user Todoist reachability probe.

    Resolves the caller's token through the same Vault building block the runtime
    uses (``resolve_connection_secret``), then does a single short-timeout GET on
    ``/projects``. Deliberately a minimal, read-only request rather than a full
    ``TodoistApiClient`` call so it fails fast instead of inheriting client retries.
    """
    try:
        from agents.agent_api.app.tools.todoist.client import TODOIST_REST_BASE_URL
        from agents.agent_api.app.user_context.secrets import resolve_connection_secret

        credential = resolve_connection_secret(telegram_user_id, "todoist")
        if not credential:
            return {"ok": False, "detail": "no token for user"}
        token = credential.secret

        request = urllib.request.Request(
            f"{TODOIST_REST_BASE_URL}/projects",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=_TODOIST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))

        # Todoist API v1 paginates as {"results": [...]}; tolerate a bare list too.
        results = data.get("results", []) if isinstance(data, dict) else data
        count = len(results) if isinstance(results, list) else 0
        return {"ok": True, "detail": f"{count} project(s)"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - health probe must never propagate
        return {"ok": False, "detail": _error_detail(exc)}


@router.get("/health/detail")
def health_detail(telegram_user_id: Optional[int] = None) -> Dict[str, Any]:
    """Deep dependency probe backing the Telegram ``/status`` card.

    Runs the DeepSeek and Todoist checks concurrently and reports an aggregate
    status plus the configured model. Unlike ``/health`` (pure liveness), this
    reflects downstream dependency health.
    """
    checks: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "deepseek": executor.submit(_check_deepseek),
            "todoist": executor.submit(_check_todoist, telegram_user_id),
        }
        for name, future in futures.items():
            try:
                checks[name] = future.result(timeout=_PROBE_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - timeout or probe crash
                checks[name] = {"ok": False, "detail": _error_detail(exc)}

    from agents.agent_api.app.run_logging import get_log_writer_stats, log_writer_healthy

    stats = get_log_writer_stats()
    checks["log_writer"] = {
        "ok": log_writer_healthy(),
        "detail": f"accepted={stats.events_accepted} dropped={stats.events_dropped} writes={stats.writes_completed} failed={stats.writes_failed}",
    }

    overall_ok = all(check.get("ok") for check in checks.values())
    return {
        "status": "ok" if overall_ok else "degraded",
        "model": DEEPSEEK_MODEL,
        "checks": checks,
        "limits": {
            "run_deadline_seconds": settings.run_deadline_seconds,
            "max_agent_turns": settings.max_agent_turns,
            "deepseek_request_timeout_seconds": settings.deepseek_request_timeout_seconds,
            "model_router_complex_timeout_seconds": (
                settings.model_router_complex_timeout_seconds
            ),
        },
    }
