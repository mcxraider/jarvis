# Notion MCP Tool Definitions

19 tools available under the `Notion:` namespace. Full name, description, and parameter schema for each.

---

## Notion:search

**Description:** Search the user's Notion workspace and connected sources (Slack, Google Drive, GitHub, Jira, Teams, SharePoint, OneDrive, Linear) and return a ranked list of results to read. Two query types: "internal" (default, content across Notion and connected sources) or "user" (find people by name/email). Use `search` when you want results to read/filter/cite/fetch in full. Backend auto-selects AI search (if user has Notion AI + connectors) or workspace-only search.

**Parameters:**
- `query` (string, required) — Semantic search query. One question/topic per call.
- `query_type` (enum: `internal`|`user`, optional)
- `content_search_mode` (enum: `workspace_search`|`ai_search`, optional)
- `data_source_url` (string, optional) — `collection://...` URL to search within a specific data source.
- `page_url` (string, optional) — restrict search to within a specific page's content.
- `teamspace_id` (string, optional)
- `filters` (object, optional) — `created_by_user_ids` (array of user IDs), `created_date_range` (`start_date`, `end_date`)
- `page_size` (integer, optional, default 10, max 25)
- `max_highlight_length` (integer, optional, default 200, 0 to omit)

---

## Notion:fetch

**Description:** Retrieves details about a Notion entity (page, database, or data source) by URL or ID. Pages return in enhanced Markdown format. Databases return all data sources (collections), each with an ID shown in `<data-source url="collection://...">` tags. Pass `"self"` as `id` to get the connected workspace + authenticated user identity.

**Parameters:**
- `id` (string, required) — URL or ID (also accepts `"self"` or `collection://...`)
- `include_discussions` (boolean, optional) — adds `<page-discussions>` summary with `discussion://` URLs
- `include_transcript` (boolean, optional) — include full meeting note transcripts

---

## Notion:notion-create-pages

**Description:** Creates one or more Notion pages with specified properties and content. All pages in one call share the same parent (a page, database, or data source). Content is a string in Notion-flavored Markdown. Supports templates (via `template_id`), icons, and covers.

**Parameters:**
- `pages` (array, required, max 100) — each item:
  - `properties` (object) — property name → value map; must include `title`
  - `content` (string) — Notion Markdown content (excluding title)
  - `template_id` (string) — template to apply (mutually exclusive with `content`)
  - `icon` (string) — emoji, custom emoji name, or external image URL; `"none"` to remove
  - `cover` (string) — external image URL; `"none"` to remove
- `parent` (object, optional) — one of `{type: "page_id", page_id}`, `{type: "database_id", database_id}`, `{type: "data_source_id", data_source_id}`. Omit for workspace-level private pages.
- `allow_async` (boolean, optional) — opt into async task result for background execution

---

## Notion:notion-update-page

**Description:** Updates a Notion page's properties or content via various commands. Supports `update_properties`, `update_content` (search-and-replace), `replace_content` (full overwrite), `insert_content` (prepend/append), `apply_template`, and `update_verification`.

**Parameters:**
- `page_id` (string, required)
- `command` (enum, required): `update_properties` | `update_content` | `replace_content` | `insert_content` | `apply_template` | `update_verification`
- `properties` (object) — required for `update_properties`
- `content_updates` (array) — required for `update_content`; each `{old_str, new_str, replace_all_matches?}`
- `new_str` (string) — required for `replace_content`
- `content` (string) — required for `insert_content`
- `position` (object) — optional for `insert_content`: `{type: "start"}` or `{type: "end"}`
- `template_id` (string) — required for `apply_template`
- `verification_status` (enum: `verified`|`unverified`) — required for `update_verification`
- `verification_expiry_days` (integer, optional)
- `allow_deleting_content` (boolean, optional) — allow deleting child pages/databases during `replace_content`/`update_content`
- `icon` / `cover` (string, optional) — can be set alongside any command
- `allow_async` (boolean, optional)

---

## Notion:notion-create-database

**Description:** Creates a new Notion database, either via raw SQL DDL (`schema`) or a canonical typed database (`database_type`: tasks/projects/skills). Returns schema and data source ID.

**Parameters:**
- `schema` (string) — `CREATE TABLE (...)` DDL; mutually exclusive with `database_type`
- `database_type` (enum: `tasks`|`projects`|`skills`)
- `title` (string, optional)
- `description` (string, optional)
- `parent` (object, optional) — `{page_id}`

---

## Notion:notion-update-data-source

**Description:** Updates a data source's schema, title, or attributes using SQL DDL (`ADD COLUMN`, `DROP COLUMN`, `RENAME COLUMN`, `ALTER COLUMN SET`).

**Parameters:**
- `data_source_id` (string, required) — `collection://...` URI or bare UUID
- `statements` (string, optional) — semicolon-separated DDL statements
- `title` (string, optional)
- `description` (string, optional)
- `is_inline` (boolean, optional)
- `in_trash` (boolean, optional)

---

## Notion:notion-query-data-sources

**Description:** Query Notion databases via SQL (SQLite syntax against data source URLs as table names) or via an existing view's filters/sorts ("view" mode).

**Parameters:**
- `data` (object, required) — one of two shapes:
  - SQL mode: `{mode: "sql", data_source_urls: [...], query: "SELECT ...", params?: [...]}`
  - View mode: `{mode: "view", view_url, is_archived?, page_size?, start_cursor?}`

---

## Notion:notion-query-database-view

**Description:** Query a database view exactly as configured (its existing filters/sorts/columns). For custom SQL or cross-source queries use `notion-query-data-sources` instead.

**Parameters:**
- `view_url` (string, required) — e.g. `https://notion.so/workspace/db-id?v=view-id`
- `is_archived` (boolean, optional)
- `page_size` (integer, optional, max 100)
- `start_cursor` (string, optional)

---

## Notion:notion-create-view

**Description:** Creates a new view on a database — either a new tab on an existing database (`database_id`) or an inline linked view on a page (`parent_page_id`). Supports table, board, list, calendar, timeline, gallery, form, chart, map, dashboard types, with a DSL for filters/sorts/grouping.

**Parameters:**
- `data_source_id` (string, required)
- `name` (string, required)
- `type` (enum, required): `table`|`board`|`list`|`calendar`|`timeline`|`gallery`|`form`|`chart`|`map`|`dashboard`
- `database_id` (string, optional, mutually exclusive w/ `parent_page_id`)
- `parent_page_id` (string, optional)
- `configure` (string, optional) — DSL: `FILTER`, `SORT BY`, `GROUP BY`, `CALENDAR BY`, `TIMELINE BY`, `MAP BY`, `CHART`, `FORM`, `SHOW`, etc.

---

## Notion:notion-update-view

**Description:** Updates an existing view's name, filters, sorts, or display configuration using the same DSL as `create-view`. Supports `CLEAR FILTER`, `CLEAR SORT`, `CLEAR GROUP BY`.

**Parameters:**
- `view_id` (string, required) — `view://` URI, Notion URL with `?v=`, or bare UUID
- `name` (string, optional)
- `configure` (string, optional)

---

## Notion:notion-move-pages

**Description:** Moves up to 100 pages or databases to a new parent (page, database, data source, or workspace root).

**Parameters:**
- `page_or_database_ids` (array of strings, required, max 100)
- `new_parent` (object, required) — one of `{type:"page_id", page_id}`, `{type:"database_id", database_id}`, `{type:"data_source_id", data_source_id}`, `{type:"workspace"}`

---

## Notion:notion-duplicate-page

**Description:** Duplicates a page within the workspace. Completes asynchronously — new page may not be populated immediately.

**Parameters:**
- `page_id` (string, required)

---

## Notion:notion-create-attachment

**Description:** Creates a small UTF-8 text attachment (HTML, Markdown, plain text, CSV, JSON, XML, CSS, YAML, TSV, calendar, GPX, SVG — max 200 KiB) and uploads it to Notion. Returns a `markdown_source` to attach via `create-pages`/`update-page` within one hour.

**Parameters:**
- `filename` (string, required) — must include supported extension
- `content` (string, required, max 200 KiB UTF-8)
- `content_type` (string, optional) — MIME type, must match extension

---

## Notion:notion-create-comment

**Description:** Adds a comment to a page, specific block content (via `selection_with_ellipsis`), or replies to an existing discussion thread (via `discussion_id`).

**Parameters:**
- `page_id` (string, required)
- `discussion_id` (string, optional) — reply to existing thread
- `selection_with_ellipsis` (string, optional) — e.g. `"# Section Ti...tle content"`
- `markdown` (string) — one of `markdown`/`rich_text` required
- `rich_text` (array of rich text objects) — alternative to `markdown`

---

## Notion:notion-get-comments

**Description:** Gets comments/discussions from a page (page-level by default; full comment content in XML format).

**Parameters:**
- `page_id` (string, required)
- `discussion_id` (string, optional) — fetch one specific discussion
- `include_all_blocks` (boolean, optional, default false)
- `include_resolved` (boolean, optional, default false)

---

## Notion:notion-get-teams

**Description:** Retrieves teamspaces in the workspace — membership status, IDs, names, roles. Max 10 results per membership-status bucket.

**Parameters:**
- `query` (string, optional) — filter by team name

---

## Notion:notion-get-users

**Description:** Retrieves workspace users (members + guests) with IDs, names, emails, and type (person/bot). Supports cursor pagination and lookup by ID (including `"self"`).

**Parameters:**
- `query` (string, optional) — filter by name/email
- `user_id` (string, optional) — fetch a specific user, or `"self"`
- `page_size` (integer, optional, default 100, max 100)
- `start_cursor` (string, optional)

---

## Notes for building your AI project

- Nearly every write operation (`create-pages`, `update-page`, `create-database`) uses **Notion-flavored Markdown** for content — the real spec lives behind an MCP resource (`notion://docs/enhanced-markdown-spec`), not hardcoded in the tool description.
- Property values use a flattened JSON map (SQLite-style), with special expansion syntax for dates, places, checkboxes, and numbers.
- `fetch` is the universal read tool — pages, databases, and data sources all go through it; database results expose child `<data-source url="collection://...">` IDs needed for querying/updating.
- Async support (`allow_async`) exists on `create-pages` and `update-page` for large operations that may exceed sync wait limits.
