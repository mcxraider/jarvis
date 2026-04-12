# Tool Search Design

`tool_search` is the discovery and lazy-loading entry point for tools in the app.

When I call `tool_search`, I pass a natural language query string and it returns up to 5 tools by default. The result count is configurable via `limit`, with a maximum of 20.

The important behavior is that `tool_search` is intent-based, not strict keyword matching. It should work like semantic or vector search over the tool registry, matching the query against tool names, descriptions, and optionally usage metadata to find the closest tools for the request.

For example, a query like `"notion create page update"` should be able to return tools such as:

- `notion-create-pages`
- `notion-update-page`
- `notion-fetch`

That should happen even when the wording in the query does not exactly match the tool names, because the system is interpreting the user’s intent rather than doing plain exact-match lookup.

## Why This Exists

The purpose of `tool_search` is context window management.

If the app injects every available tool into the model context upfront, the prompt becomes unnecessarily large and expensive. With 40+ tools, this quickly wastes tokens and makes tool selection noisier.

`tool_search` solves that by acting as a just-in-time tool loader:

1. The model describes what it needs in natural language.
2. `tool_search` retrieves the most relevant tools for that intent.
3. Only those tools are injected into the active model context.
4. The model can then call the loaded tools directly.

This keeps the working toolset small, relevant, and cheaper to operate.

## Expected Runtime Behavior

Once a tool has been loaded through `tool_search`, it should remain available for the rest of the conversation session.

That means:

- I do not need to call `tool_search` again for the same tool in later turns.
- The app should maintain a per-conversation registry of already-loaded tools.
- Future tool calls can reference those tools directly without re-discovery.

This is the intended workflow:

1. Call `tool_search` once for a capability area such as Notion.
2. Load the returned tools into the conversation context.
3. Reuse those tools freely across subsequent turns.

## Recommended API Shape

Suggested request:

```json
{
  "query": "notion create page update",
  "limit": 5
}
```

Suggested response shape:

```json
{
  "ok": true,
  "query": "notion create page update",
  "limit": 5,
  "results": [
    {
      "name": "notion-create-pages",
      "description": "Create one or more new Notion pages under a given parent.",
      "score": 0.93
    },
    {
      "name": "notion-update-page",
      "description": "Update an existing Notion page's properties or content.",
      "score": 0.91
    }
  ],
  "loaded_tools": [
    "notion-create-pages",
    "notion-update-page"
  ]
}
```

The `score` field is optional, but useful for debugging ranking quality. The `loaded_tools` field is also helpful because it makes the side effect explicit: these tools are now active in the session.

## Recommended Matching Strategy

The ranking layer should be semantic-first, with lexical signals as a secondary boost.

Good candidates for retrieval signals:

- Tool name
- Tool description
- Parameter names
- Short usage examples
- Domain tags such as `notion`, `github`, `calendar`, `search`

In practice, the strongest implementation pattern is:

1. Embed a tool document for each registered tool.
2. Run vector similarity against the user query.
3. Optionally rerank top candidates with keyword overlap or lightweight scoring rules.
4. Return the top `N` tools and inject them into context.

This gives you semantic recall without losing precision on obvious exact matches.

## Conversation State Rules

The app should track loaded tools separately from the global registry.

Recommended model:

- Global registry: every tool the application knows about
- Session registry: tools already loaded for the current conversation
- Active context payload: the subset actually sent to the model on the next turn

Suggested behavior:

- If a requested tool is already loaded in the session, do not reload it.
- If `tool_search` returns a tool already present, deduplicate it.
- Preserve loaded tools across turns until the conversation ends or is explicitly reset.

## Guidance For Model Use

In future prompts and agent logic, `tool_search` should be treated as a discovery step, not a normal business tool.

The model should use it when:

- It needs a tool family that is not yet loaded
- The user asks for a capability by intent rather than exact tool name
- The available tools in context are insufficient for the task

The model should avoid using it when:

- The needed tool is already loaded
- The exact tool is already known and available
- No tool is required at all

## Example Interaction Pattern

1. Model decides it needs Notion authoring tools.
2. Model calls:

```json
{
  "query": "notion create page update",
  "limit": 5
}
```

3. App returns the most relevant Notion tools and marks them as loaded.
4. On later turns, the model directly calls `notion-create-pages` or `notion-update-page` without repeating `tool_search`.

## Telegram Progress Updates

For future updates, one good UX pattern is to show the user a concise but still step-by-step summary of what the agent is doing in the background.

This works especially well in Telegram, where users benefit from seeing visible progress without being flooded with raw logs.

The style should feel:

- concise in formatting
- explicit about each step
- slightly verbose in explanation
- reassuring about what is happening next
- written in plain language rather than internal tool jargon

Recommended pattern:

1. Show a short phase label such as `Search`, `Fetch`, or `Update`.
2. Under that label, show one sentence explaining what happened or what the agent is doing now.
3. Continue appending steps as the workflow progresses.
4. End with a compact completion summary describing the final result.

Example style:

```text
Search
Found the Tool calling page. Let me fetch it first to see the existing content.

Fetch
Page is empty. Adding the code now.

Notion-update-page
Done — added to your Tool calling page inside jarvis-mcp. It's structured with separate sections for setup, tool definitions, API calls, the markdown converter, dispatcher, and the agentic loop, plus notes at the bottom.
```

This is a strong default format because it gives the user:

- visibility into the current step
- confidence that the agent is making progress
- enough detail to understand decisions
- a readable narrative instead of noisy debug output

Implementation guidance:

- Do not stream raw tool payloads directly to Telegram.
- Convert tool activity into user-facing progress sentences.
- Prefer one short update per meaningful state transition.
- Keep the wording action-oriented and easy to scan.
- Include the final outcome, not just intermediate steps.

## Implementation Notes

If you are building this into your app, these behaviors are worth preserving:

- Default `limit` to 5.
- Cap `limit` at 20.
- Return semantically relevant tools, not just exact matches.
- Cache loaded tools at the conversation level.
- Inject only newly loaded tools into subsequent model calls.
- Keep discovery separate from execution so ranking logic stays easy to tune.
- Support a user-facing progress layer so internal tool execution can be translated into readable Telegram status updates.

## Suggested Pseudocode

```python
def tool_search(query: str, limit: int = 5, conversation_id: str | None = None) -> dict:
    limit = max(1, min(limit, 20))

    ranked_tools = semantic_search_tool_registry(query=query, limit=limit)

    loaded = []
    for tool in ranked_tools:
        if conversation_id is not None and not is_tool_loaded(conversation_id, tool["name"]):
            mark_tool_loaded(conversation_id, tool["name"])
            loaded.append(tool["name"])

    return {
        "ok": True,
        "query": query,
        "limit": limit,
        "results": ranked_tools,
        "loaded_tools": loaded,
    }
```

## Design Principle

`tool_search` should be implemented as a semantic discovery layer plus a session-aware lazy loader.

That combination is what makes the pattern work well:

- semantic search finds the right tools from intent
- lazy loading protects the context window
- session persistence prevents redundant searches

That is the behavior to preserve in the app going forward.
