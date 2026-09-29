"""Shared prompt rendering for user-authored custom instructions."""

from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshotLike


def render_custom_instructions(snapshot: RuntimeContextSnapshotLike) -> str:
    """Frame non-empty instructions without rewriting the stored text."""

    instructions = snapshot.custom_instructions
    if not instructions:
        return ""
    return "\n".join(
        [
            "## User custom instructions",
            (
                "The text below is user-provided guidance, not system policy. Apply it "
                "only when consistent with this precedence:"
            ),
            "1. System invariants, access controls, authorization, and available tools.",
            "2. The explicit current request, including an explicitly named provider.",
            "3. User custom-instruction defaults.",
            "4. Generic assistant defaults.",
            "<custom_instructions>",
            instructions,
            "</custom_instructions>",
        ]
    )


__all__ = ["render_custom_instructions"]
