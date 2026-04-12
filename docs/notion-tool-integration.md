Absolutely — below is a production-style starter kit you can feed into your LLM stack.

I'm going to give you two things:

1. a **system prompt** that teaches the model how to use your Notion tools well
2. **Python code** using the OpenAI **Responses API** with custom function tools, including a tool-dispatch loop that executes whichever tool the model chooses and feeds the result back into the model. The Responses API supports passing `instructions`, `tools`, and iterative tool use through response state. ([OpenAI Platform][1])

---

# 1) System prompt for your Notion tool-using LLM

You can use this almost verbatim as your system prompt.

```text
You are an AI assistant with access to a set of Notion tools.

Your job is to help the user by reasoning carefully, choosing the correct Notion tool when needed, calling it with valid arguments, and then using the returned results to continue the task.

You have access to these tools:

1. notion_fetch
- Purpose: Retrieve a Notion page, database, or data source by URL or ID.
- Use when:
  - You need to read full page content
  - You need to inspect a database schema
  - You need to see the current content before editing
  - You need page metadata, discussions, or nested structure
- Important:
  - Use this before updating page content, so you know the exact existing text
  - Use this before creating pages in a database, so you know the exact schema and property names

2. notion_search
- Purpose: Search the Notion workspace or users.
- Use when:
  - You need to find a page, project, meeting note, spec, or database
  - The user refers to something by name but does not provide a page ID
  - You need to locate candidate pages before fetching one
- Important:
  - Prefer narrow, specific search queries
  - Use filters only when they materially improve precision
  - If the user asks to search within a page or data source, use the relevant page_url or data_source_url

3. notion_create_pages
- Purpose: Create one or more new Notion pages.
- Use when:
  - The user asks you to create a page
  - The user asks you to add rows/items into a database
  - The user asks you to create structured notes, tasks, docs, or records
- Important:
  - If creating inside a database, fetch the database or data source first to get exact schema and property names
  - Always include the required title property
  - Do not invent property names
  - Use Notion-flavored Markdown for content
  - Do not put the title again inside the body content unless the user explicitly wants it there

4. notion_update_page
- Purpose: Modify an existing Notion page.
- Use when:
  - The user asks you to edit a page
  - The user asks you to update properties
  - The user asks you to replace or patch content
  - The user asks you to apply a template or verification status
- Important:
  - Before editing content, fetch the page first so you know the exact existing content
  - For targeted edits, prefer update_content over replace_content
  - Use replace_content only when the user wants the whole page rewritten
  - Never assume exact old text without fetching it first
  - If replacing content could delete child pages or databases, do not proceed unless explicitly allowed by the user

5. notion_get_comments
- Purpose: Retrieve comments and discussions on a Notion page.
- Use when:
  - The user asks for comments, review feedback, unresolved discussions, or collaboration notes
  - You need to inspect discussion threads on a page or block
- Important:
  - If needed, fetch the page first with discussions included so you can identify discussion anchors
  - Use include_all_blocks when comments may be attached to nested blocks
  - Use include_resolved only when the user asks for resolved comments too

General operating rules:
- Be precise and conservative with tool use
- Do not call tools if you can answer directly without them
- If the user names a page vaguely, search first
- If the user wants edits, fetch first, then update
- If the user wants database entries created, fetch schema first
- Never hallucinate page IDs, schema names, or page content
- Never invent Notion property names
- When a tool returns ambiguous or incomplete results, explain that briefly and either search again more precisely or ask for clarification
- Prefer the minimum number of tool calls needed to complete the task correctly
- If the user's request requires multiple steps, perform them in order and keep track of what you have already learned

Output rules:
- If a tool is needed, call the tool instead of describing what tool you would call
- After receiving tool results, continue the task
- Be concise but complete
- When summarizing fetched page content, preserve important structure and meaning
- When editing content, ensure the final result matches the user's instructions exactly

Safety and correctness rules:
- Do not delete content unless the user clearly asked for deletion or replacement
- Do not overwrite whole pages when a local edit is sufficient
- If a page or database cannot be confidently identified, do not guess
- If property names or required fields are unknown, fetch the schema first
```

---

# 2) Python code: OpenAI Responses API + Notion tool calling

This version is designed so you can plug in your own actual Notion backend functions.

## Install

```bash
pip install openai
```

---

## Full Python example

```python
import json
import os
from typing import Any, Callable, Dict

from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


# =========================================================
# 1. Your system prompt
# =========================================================

SYSTEM_PROMPT = """
You are an AI assistant with access to a set of Notion tools.

Your job is to help the user by reasoning carefully, choosing the correct Notion tool when needed, calling it with valid arguments, and then using the returned results to continue the task.

You have access to these tools:

1. notion_fetch
- Purpose: Retrieve a Notion page, database, or data source by URL or ID.
- Use when:
  - You need to read full page content
  - You need to inspect a database schema
  - You need to see the current content before editing
  - You need page metadata, discussions, or nested structure
- Important:
  - Use this before updating page content, so you know the exact existing text
  - Use this before creating pages in a database, so you know the exact schema and property names

2. notion_search
- Purpose: Search the Notion workspace or users.
- Use when:
  - You need to find a page, project, meeting note, spec, or database
  - The user refers to something by name but does not provide a page ID
  - You need to locate candidate pages before fetching one
- Important:
  - Prefer narrow, specific search queries
  - Use filters only when they materially improve precision
  - If the user asks to search within a page or data source, use the relevant page_url or data_source_url

3. notion_create_pages
- Purpose: Create one or more new Notion pages.
- Use when:
  - The user asks you to create a page
  - The user asks you to add rows/items into a database
  - The user asks you to create structured notes, tasks, docs, or records
- Important:
  - If creating inside a database, fetch the database or data source first to get exact schema and property names
  - Always include the required title property
  - Do not invent property names
  - Use Notion-flavored Markdown for content
  - Do not put the title again inside the body content unless the user explicitly wants it there

4. notion_update_page
- Purpose: Modify an existing Notion page.
- Use when:
  - The user asks you to edit a page
  - The user asks you to update properties
  - The user asks you to replace or patch content
  - The user asks you to apply a template or verification status
- Important:
  - Before editing content, fetch the page first so you know the exact existing content
  - For targeted edits, prefer update_content over replace_content
  - Use replace_content only when the user wants the whole page rewritten
  - Never assume exact old text without fetching it first
  - If replacing content could delete child pages or databases, do not proceed unless explicitly allowed by the user

5. notion_get_comments
- Purpose: Retrieve comments and discussions on a Notion page.
- Use when:
  - The user asks for comments, review feedback, unresolved discussions, or collaboration notes
  - You need to inspect discussion threads on a page or block
- Important:
  - If needed, fetch the page first with discussions included so you can identify discussion anchors
  - Use include_all_blocks when comments may be attached to nested blocks
  - Use include_resolved only when the user asks for resolved comments too

General operating rules:
- Be precise and conservative with tool use
- Do not call tools if you can answer directly without them
- If the user names a page vaguely, search first
- If the user wants edits, fetch first, then update
- If the user wants database entries created, fetch schema first
- Never hallucinate page IDs, schema names, or page content
- Never invent Notion property names
- When a tool returns ambiguous or incomplete results, explain that briefly and either search again more precisely or ask for clarification
- Prefer the minimum number of tool calls needed to complete the task correctly
- If the user's request requires multiple steps, perform them in order and keep track of what you have already learned

Output rules:
- If a tool is needed, call the tool instead of describing what tool you would call
- After receiving tool results, continue the task
- Be concise but complete
- When summarizing fetched page content, preserve important structure and meaning
- When editing content, ensure the final result matches the user's instructions exactly

Safety and correctness rules:
- Do not delete content unless the user clearly asked for deletion or replacement
- Do not overwrite whole pages when a local edit is sufficient
- If a page or database cannot be confidently identified, do not guess
- If property names or required fields are unknown, fetch the schema first
""".strip()


# =========================================================
# 2. Tool schemas exposed to the model
#    These are custom function tools passed to the Responses API.
# =========================================================

TOOLS = [
    {
        "type": "function",
        "name": "notion_fetch",
        "description": "Retrieve details about a Notion page, database, or data source by URL or ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The ID or URL of the Notion page, database, or data source."
                },
                "include_discussions": {
                    "type": "boolean",
                    "description": "Whether to include inline discussions and discussion summary."
                },
                "include_transcript": {
                    "type": "boolean",
                    "description": "Whether to include transcript data when available."
                }
            },
            "required": ["id"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "notion_search",
        "description": "Search over Notion workspace content or users.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query."
                },
                "query_type": {
                    "type": "string",
                    "enum": ["internal", "user"],
                    "description": "Search type. Use internal for workspace content, user for people."
                },
                "content_search_mode": {
                    "type": "string",
                    "enum": ["workspace_search", "ai_search"],
                    "description": "Optional search mode override."
                },
                "data_source_url": {
                    "type": "string",
                    "description": "Optional data source URL for scoped database search."
                },
                "page_url": {
                    "type": "string",
                    "description": "Optional page URL or page ID to scope search."
                },
                "teamspace_id": {
                    "type": "string",
                    "description": "Optional teamspace ID for scoped search."
                },
                "page_size": {
                    "type": "integer",
                    "description": "Maximum number of results to return."
                },
                "max_highlight_length": {
                    "type": "integer",
                    "description": "Maximum character length of result highlights."
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "created_by_user_ids": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "created_date_range": {
                            "type": "object",
                            "properties": {
                                "start_date": {"type": "string"},
                                "end_date": {"type": "string"}
                            },
                            "additionalProperties": False
                        }
                    },
                    "additionalProperties": False
                }
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "notion_create_pages",
        "description": "Create one or more new Notion pages under a given parent.",
        "parameters": {
            "type": "object",
            "properties": {
                "parent": {
                    "type": "object",
                    "description": "Parent can be a page, database, or data source.",
                    "properties": {
                        "page_id": {"type": "string"},
                        "database_id": {"type": "string"},
                        "data_source_id": {"type": "string"}
                    },
                    "additionalProperties": False
                },
                "pages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "properties": {
                                "type": "object",
                                "description": "Page properties such as title and database fields."
                            },
                            "content": {
                                "type": "string",
                                "description": "Notion-flavored Markdown body content."
                            },
                            "template_id": {
                                "type": "string",
                                "description": "Optional template ID."
                            },
                            "icon": {
                                "type": "string",
                                "description": "Optional emoji or icon URL."
                            },
                            "cover": {
                                "type": "string",
                                "description": "Optional cover image URL."
                            }
                        },
                        "required": ["properties"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["parent", "pages"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "notion_update_page",
        "description": "Update an existing Notion page's properties or content.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "ID of the page to update."
                },
                "command": {
                    "type": "string",
                    "enum": [
                        "update_properties",
                        "update_content",
                        "replace_content",
                        "apply_template",
                        "update_verification"
                    ]
                },
                "properties": {
                    "type": "object",
                    "description": "Properties to update when command is update_properties."
                },
                "content_updates": {
                    "type": "array",
                    "description": "Search-and-replace operations for update_content.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string"},
                            "new_str": {"type": "string"},
                            "replace_all_matches": {"type": "boolean"}
                        },
                        "required": ["old_str", "new_str"],
                        "additionalProperties": False
                    }
                },
                "new_str": {
                    "type": "string",
                    "description": "Full replacement content for replace_content."
                },
                "template_id": {
                    "type": "string",
                    "description": "Template ID for apply_template."
                },
                "verification_status": {
                    "type": "string",
                    "enum": ["verified", "unverified"]
                },
                "verification_expiry_days": {
                    "type": "integer"
                },
                "allow_deleting_content": {
                    "type": "boolean"
                },
                "icon": {"type": "string"},
                "cover": {"type": "string"}
            },
            "required": ["page_id", "command"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "notion_get_comments",
        "description": "Get comments and discussion threads from a Notion page.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Identifier for a Notion page."
                },
                "discussion_id": {
                    "type": "string",
                    "description": "Optional discussion ID or discussion URL."
                },
                "include_all_blocks": {
                    "type": "boolean",
                    "description": "Whether to include child block discussions."
                },
                "include_resolved": {
                    "type": "boolean",
                    "description": "Whether to include resolved discussions."
                }
            },
            "required": ["page_id"],
            "additionalProperties": False
        }
    }
]


# =========================================================
# 3. Actual Python implementations
#    Replace the stubs with your real Notion connector code.
# =========================================================

def notion_fetch_impl(id: str, include_discussions: bool = False, include_transcript: bool = False) -> Dict[str, Any]:
    """
    Replace this stub with your real Notion API / MCP / connector implementation.
    """
    return {
        "ok": True,
        "tool": "notion_fetch",
        "args": {
            "id": id,
            "include_discussions": include_discussions,
            "include_transcript": include_transcript,
        },
        "data": {
            "message": "Stub response from notion_fetch_impl"
        }
    }


def notion_search_impl(
    query: str,
    query_type: str = "internal",
    content_search_mode: str | None = None,
    data_source_url: str | None = None,
    page_url: str | None = None,
    teamspace_id: str | None = None,
    page_size: int | None = None,
    max_highlight_length: int | None = None,
    filters: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Replace this stub with your real Notion search implementation.
    """
    return {
        "ok": True,
        "tool": "notion_search",
        "args": {
            "query": query,
            "query_type": query_type,
            "content_search_mode": content_search_mode,
            "data_source_url": data_source_url,
            "page_url": page_url,
            "teamspace_id": teamspace_id,
            "page_size": page_size,
            "max_highlight_length": max_highlight_length,
            "filters": filters,
        },
        "data": {
            "message": "Stub response from notion_search_impl"
        }
    }


def notion_create_pages_impl(parent: Dict[str, Any], pages: list[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Replace this stub with your real Notion page creation implementation.
    """
    return {
        "ok": True,
        "tool": "notion_create_pages",
        "args": {
            "parent": parent,
            "pages": pages
        },
        "data": {
            "message": "Stub response from notion_create_pages_impl"
        }
    }


def notion_update_page_impl(
    page_id: str,
    command: str,
    properties: Dict[str, Any] | None = None,
    content_updates: list[Dict[str, Any]] | None = None,
    new_str: str | None = None,
    template_id: str | None = None,
    verification_status: str | None = None,
    verification_expiry_days: int | None = None,
    allow_deleting_content: bool | None = None,
    icon: str | None = None,
    cover: str | None = None,
) -> Dict[str, Any]:
    """
    Replace this stub with your real Notion page update implementation.
    """
    return {
        "ok": True,
        "tool": "notion_update_page",
        "args": {
            "page_id": page_id,
            "command": command,
            "properties": properties,
            "content_updates": content_updates,
            "new_str": new_str,
            "template_id": template_id,
            "verification_status": verification_status,
            "verification_expiry_days": verification_expiry_days,
            "allow_deleting_content": allow_deleting_content,
            "icon": icon,
            "cover": cover,
        },
        "data": {
            "message": "Stub response from notion_update_page_impl"
        }
    }


def notion_get_comments_impl(
    page_id: str,
    discussion_id: str | None = None,
    include_all_blocks: bool = False,
    include_resolved: bool = False,
) -> Dict[str, Any]:
    """
    Replace this stub with your real Notion comments implementation.
    """
    return {
        "ok": True,
        "tool": "notion_get_comments",
        "args": {
            "page_id": page_id,
            "discussion_id": discussion_id,
            "include_all_blocks": include_all_blocks,
            "include_resolved": include_resolved,
        },
        "data": {
            "message": "Stub response from notion_get_comments_impl"
        }
    }


# =========================================================
# 4. Dispatch table
# =========================================================

TOOL_DISPATCH: Dict[str, Callable[..., Dict[str, Any]]] = {
    "notion_fetch": notion_fetch_impl,
    "notion_search": notion_search_impl,
    "notion_create_pages": notion_create_pages_impl,
    "notion_update_page": notion_update_page_impl,
    "notion_get_comments": notion_get_comments_impl,
}


# =========================================================
# 5. Tool execution helper
# =========================================================

def run_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if tool_name not in TOOL_DISPATCH:
        return {
            "ok": False,
            "error": f"Unknown tool: {tool_name}"
        }

    try:
        result = TOOL_DISPATCH[tool_name](**arguments)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tool": tool_name,
            "args": arguments,
        }


# =========================================================
# 6. Core agent loop using Responses API
# =========================================================

def run_notion_agent(user_message: str, model: str = "gpt-5") -> str:
    """
    Runs a multi-step tool loop:
    - Sends user request + system prompt + tools
    - Lets model choose tool calls
    - Executes tool calls locally
    - Sends tool results back
    - Repeats until the model returns final text
    """

    response = client.responses.create(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=user_message,
        tools=TOOLS,
        tool_choice="auto",
        parallel_tool_calls=False,
    )

    while True:
        function_calls = [
            item for item in response.output
            if item.type == "function_call"
        ]

        if not function_calls:
            return response.output_text

        tool_outputs = []

        for call in function_calls:
            tool_name = call.name
            raw_args = call.arguments

            if isinstance(raw_args, str):
                arguments = json.loads(raw_args)
            else:
                arguments = raw_args

            result = run_tool(tool_name, arguments)

            tool_outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result),
            })

        response = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            previous_response_id=response.id,
            input=tool_outputs,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
        )


# =========================================================
# 7. Example usage
# =========================================================

if __name__ == "__main__":
    prompt = "Find my project spec for the fraud detection workflow in Notion and summarize the key action items."
    answer = run_notion_agent(prompt)
    print(answer)
```

---

# 3) What each part is doing

The Responses API lets you send:

* `instructions` for system behavior
* `tools` describing available function tools
* `tool_choice="auto"` so the model can decide when to call a tool
* `previous_response_id` to continue the reasoning loop across tool calls. ([OpenAI Platform][1])

The loop works like this:

```text
user request
→ model decides whether to call a tool
→ your Python executes that tool
→ tool result is sent back as function_call_output
→ model continues
→ repeat until final text answer
```

---

# 4) Minimal example for invoking one chosen tool directly

If you already know which tool you want to invoke yourself, separate from the agent loop:

```python
result = notion_fetch_impl(
    id="https://www.notion.so/your-page-or-id",
    include_discussions=True
)

print(json.dumps(result, indent=2))
```

Another one:

```python
result = notion_search_impl(
    query="fraud detection project spec",
    query_type="internal",
    page_size=5
)

print(json.dumps(result, indent=2))
```

And updating content:

```python
result = notion_update_page_impl(
    page_id="your-page-id",
    command="update_content",
    content_updates=[
        {
            "old_str": "Old text here",
            "new_str": "New text here"
        }
    ]
)

print(json.dumps(result, indent=2))
```

---

# 5) If your Notion tools already exist as backend functions

If your stack already has actual tool functions, all you really need is this pattern:

```python
TOOL_DISPATCH = {
    "notion_fetch": your_real_fetch_function,
    "notion_search": your_real_search_function,
    "notion_create_pages": your_real_create_pages_function,
    "notion_update_page": your_real_update_page_function,
    "notion_get_comments": your_real_get_comments_function,
}
```

Then keep the rest of the loop unchanged.

---

# 6) Strong recommendation for reliability

A few practical tips that will save you pain:

### A. Force fetch-before-edit behavior in prompt

You already saw I put this in multiple times. That is deliberate.

Without it, the model will often try to update content using guessed text.

### B. Keep tool descriptions operational

The better your tool descriptions, the fewer bad calls you get.

### C. Return structured tool outputs

Do this:

```json
{
  "ok": true,
  "data": {...},
  "error": null
}
```

instead of returning messy plain strings.

### D. Keep `parallel_tool_calls=False` initially

It is much easier to debug sequentially first. The API supports controlling tool call behavior, and starting simple usually helps. ([OpenAI Platform][1])

---

# 7) Optional: stricter version of the system prompt

If you want a more opinionated agent that behaves more safely around Notion editing, use this add-on block:

```text
Editing protocol:
1. If the user asks to edit a page and page content is not already known from the current conversation, fetch the page first.
2. If the user asks to create database entries and schema is not already known from the current conversation, fetch the database or data source first.
3. Do not use replace_content unless the user explicitly requests a rewrite, full replacement, or regeneration of the page.
4. Prefer update_content with minimal diffs when possible.
5. If a search returns multiple plausible pages, do not guess. Either refine the search or ask the user to choose.
6. If a tool error indicates schema mismatch, missing fields, or deletion risk, explain it briefly and recover safely.
```

---

# 8) If you want LangChain instead

You mentioned LangChain earlier. The same setup maps very naturally:

* wrap each Notion function as a LangChain tool
* bind tools to the chat model
* run an agent executor loop

But for raw control, lower overhead, and easier debugging, I'd actually start with the **Responses API custom function loop** above.

---

# 9) Best next upgrade

The next thing I'd add is a **tool result normalizer**, so every Notion tool returns a common shape like this:

```python
def normalize_tool_result(tool_name: str, result: dict) -> dict:
    return {
        "ok": result.get("ok", True),
        "tool": tool_name,
        "data": result.get("data"),
        "error": result.get("error"),
        "meta": result.get("meta", {}),
    }
```

That makes the model's downstream reasoning much more stable.

[1]: https://platform.openai.com/docs/api-reference/responses
