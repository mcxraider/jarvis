"""Liveness and provider-neutral detailed dependency health routes."""

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import APIRouter

from agents.agent_api.app.api.schemas import DetailedHealthResponse
from agents.agent_api.app.config import settings
from agents.agent_api.app.llm.provider import LLMProviderProfile

router = APIRouter()

# Each probe has its own short network timeout; this is a backstop for a worker
# that stalls before the request begins.
_PROBE_TIMEOUT_SECONDS = 8.0
_LLM_HEALTH_TIMEOUT_SECONDS = 5.0
_TODOIST_TIMEOUT_SECONDS = 5.0


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _error_detail(exc: Exception) -> str:
    """Return a privacy-safe dependency detail without response bodies or URLs."""

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return f"HTTP {status_code}"
    return exc.__class__.__name__


def _check_llm(profile: Optional[LLMProviderProfile] = None) -> Dict[str, Any]:
    """Probe the selected orchestrator profile without spending completion tokens."""

    selected = profile or settings.orchestrator_llm
    if not selected.api_key:
        return {"ok": False, "detail": "no API key"}
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=selected.api_key,
            base_url=selected.base_url,
            timeout=min(
                selected.request_timeout_seconds,
                _LLM_HEALTH_TIMEOUT_SECONDS,
            ),
            max_retries=0,
        )
        try:
            client.models.list()
        finally:
            client.close()
        return {"ok": True, "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001 - health must always resolve
        return {"ok": False, "detail": _error_detail(exc)}


def _check_deepseek() -> Dict[str, Any]:
    """Temporary compatibility alias for older tests and operational imports."""

    return _check_llm(settings.orchestrator_llm)


def _check_todoist(telegram_user_id: Optional[int]) -> Dict[str, Any]:
    """Probe the requesting user's Todoist connection with a read-only request."""

    try:
        from agents.agent_api.app.tools.todoist.client import TODOIST_REST_BASE_URL
        from agents.agent_api.app.user_context.secrets import resolve_connection_secret

        credential = resolve_connection_secret(telegram_user_id, "todoist")
        if not credential:
            return {"ok": False, "detail": "no token for user"}

        request = urllib.request.Request(
            f"{TODOIST_REST_BASE_URL}/projects",
            headers={"Authorization": f"Bearer {credential.secret}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=_TODOIST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))

        results = data.get("results", []) if isinstance(data, dict) else data
        count = len(results) if isinstance(results, list) else 0
        return {"ok": True, "detail": f"{count} project(s)"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - health must always resolve
        return {"ok": False, "detail": _error_detail(exc)}


@router.get("/health/detail", response_model=DetailedHealthResponse)
def health_detail(telegram_user_id: Optional[int] = None) -> Dict[str, Any]:
    """Report selected orchestrator identity and downstream dependency health."""

    profile = settings.orchestrator_llm
    checks: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "llm": executor.submit(_check_llm, profile),
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
        "detail": (
            f"accepted={stats.events_accepted} dropped={stats.events_dropped} "
            f"writes={stats.writes_completed} failed={stats.writes_failed}"
        ),
    }

    provider = getattr(profile.provider, "value", profile.provider)
    return {
        "status": "ok" if all(check["ok"] for check in checks.values()) else "degraded",
        "provider": str(provider),
        "model": profile.model,
        "checks": checks,
        "limits": {
            "run_deadline_seconds": settings.run_deadline_seconds,
            "max_agent_turns": settings.max_agent_turns,
            "llm_request_timeout_seconds": profile.request_timeout_seconds,
            "model_router_complex_timeout_seconds": (
                settings.model_router_complex_timeout_seconds
            ),
        },
    }
