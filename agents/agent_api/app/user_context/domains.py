"""Pure domain-availability classification.

Given the registered adapters, the user's integration connections, and a way to
resolve a secret, decide each domain's availability and build the credentials for
the active ones. This is deliberately free of database and network access so the
branchiest logic in the runtime — active vs. the five unavailable reasons vs.
unsupported — is a table-driven unit test.

Availability rules (mirrors ``plans/scalable-user-context-integrations.md``):

- adapter + enabled + connected + secret resolves      -> ``active``
- no connection row                                    -> ``unavailable: not_connected``
- connection disabled                                  -> ``unavailable: disabled``
- connected but non-``connected`` status               -> ``unavailable: <status>`` (e.g. needs_reauth)
- connected + enabled but secret cannot be resolved    -> ``unavailable: credential_unavailable``
- a connection whose provider has no registered adapter -> ``unsupported: no_adapter``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.user_context.runtime import DomainAvailability

# (provider) -> secret string, or None when it cannot be resolved. The resolver
# supplies a closure over its open cursor; tests supply a plain dict lookup.
SecretResolver = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class ConnectionRow:
    """The slice of an ``integration_connections`` row classification needs."""

    connection_id: str
    provider: str
    status: str
    is_enabled: bool


def classify_domains(
    adapters: Dict[str, Any],
    connections: Dict[str, ConnectionRow],
    secret_resolver: SecretResolver,
) -> Tuple[List[DomainAvailability], Dict[str, IntegrationCredential]]:
    """Classify every adapter and surface unsupported connections.

    ``adapters`` maps provider -> object exposing ``.capabilities``; only that
    attribute is read, so tests can pass a lightweight stand-in. Returns the
    ordered availability list (adapters first, in adapter order, then unsupported
    connections) and the credentials for active domains only.
    """

    domains: List[DomainAvailability] = []
    credentials: Dict[str, IntegrationCredential] = {}

    for provider, adapter in adapters.items():
        capabilities = list(getattr(adapter, "capabilities", []))
        row = connections.get(provider)

        if row is None:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason="not_connected",
                    capabilities=capabilities,
                )
            )
            continue
        if not row.is_enabled:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason="disabled",
                    connection_id=row.connection_id,
                    capabilities=capabilities,
                )
            )
            continue
        if row.status != "connected":
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason=row.status,
                    connection_id=row.connection_id,
                    capabilities=capabilities,
                )
            )
            continue

        secret = secret_resolver(provider)
        if not secret:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason="credential_unavailable",
                    connection_id=row.connection_id,
                    capabilities=capabilities,
                )
            )
            continue

        domains.append(
            DomainAvailability(
                provider=provider,
                status="active",
                connection_id=row.connection_id,
                capabilities=capabilities,
            )
        )
        credentials[provider] = IntegrationCredential(
            connection_id=row.connection_id,
            provider=provider,
            secret=secret,
        )

    for provider, row in connections.items():
        if provider in adapters:
            continue
        domains.append(
            DomainAvailability(
                provider=provider,
                status="unsupported",
                reason="no_adapter",
                connection_id=row.connection_id,
            )
        )

    return domains, credentials


__all__ = ["ConnectionRow", "SecretResolver", "classify_domains"]
