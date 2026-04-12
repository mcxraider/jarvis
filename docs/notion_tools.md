Here are all 14 tools in that format:

```json
[
  {
    "name": "Notion:fetch",
    "parameters": {
      "id": "string - The ID or URL of the Notion page, database, or data source to fetch. Supports notion.so URLs, Notion Sites URLs (*.notion.site), raw UUIDs, and data source URLs (collection://...).",
      "include_discussions": "boolean - Set to true to see discussion counts and inline discussion markers that correlate with the get_comments tool.",
      "include_transcript": "boolean"
    }
  },
  {
    "name": "Notion:notion-create-comment",
    "parameters": {
      "page_id": "string - The ID of the page to comment on.",
      "content": "string - The comment content in Notion Markdown format.",
      "discussion_id": "string - Optional. To reply to an existing discussion thread."
    }
  },
  {
    "name": "Notion:notion-create-database",
    "parameters": {
      "schema": "string - SQL DDL CREATE TABLE statement defining the database schema. Column names must be double-quoted, type options use single quotes.",
      "parent": "object {page_id, type} - The parent under which to create the new database. If omitted, created as a private page at the workspace level.",
      "title": "string - Optional. The title of the new database.",
      "description": "string - Optional. The description of the new database."
    }
  },
  {
    "name": "Notion:notion-create-pages",
    "parameters": {
      "pages": "array [object {properties, content, template_id, icon, cover}] - The pages to create.",
      "parent": "any - The parent under which the new pages will be created. This can be a page (page_id), a database page (database_id), or a data source/collection under a database (data_source_id). If omitted, pages are created as private pages at the workspace level."
    }
  },
  {
    "name": "Notion:notion-create-view",
    "parameters": {
      "database_id": "string - The ID of the database to create a view on.",
      "name": "string - The name of the new view.",
      "configure": "string - View configuration DSL string. Supports FILTER, SORT BY, GROUP BY, CALENDAR BY, TIMELINE BY, MAP BY, CHART, FORM, SHOW, HIDE, COVER, WRAP CELLS, FREEZE COLUMNS directives."
    }
  },
  {
    "name": "Notion:notion-duplicate-page",
    "parameters": {
      "page_id": "string - The ID of the page to duplicate.",
      "parent_id": "string - Optional. The ID of the parent page or database to duplicate into. If omitted, duplicates in place."
    }
  },
  {
    "name": "Notion:notion-get-comments",
    "parameters": {
      "page_id": "string - Identifier for a Notion page.",
      "discussion_id": "string - Optional. Fetch a specific discussion by ID or discussion URL (e.g., discussion://pageId/blockId/discussionId).",
      "include_all_blocks": "boolean - Optional. Include discussions on child blocks (default: false).",
      "include_resolved": "boolean - Optional. Include resolved discussions (default: false)."
    }
  },
  {
    "name": "Notion:notion-get-teams",
    "parameters": {}
  },
  {
    "name": "Notion:notion-get-users",
    "parameters": {}
  },
  {
    "name": "Notion:notion-move-pages",
    "parameters": {
      "page_ids": "array [string] - The IDs of the pages or databases to move.",
      "parent_id": "string - The ID of the new parent page or database to move the pages into."
    }
  },
  {
    "name": "Notion:notion-update-data-source",
    "parameters": {
      "data_source_id": "string - The ID of the data source to update.",
      "schema": "string - SQL DDL ALTER TABLE or CREATE TABLE statement defining the updated schema.",
      "title": "string - Optional. New title for the data source.",
      "description": "string - Optional. New description for the data source."
    }
  },
  {
    "name": "Notion:notion-update-page",
    "parameters": {
      "page_id": "string - The ID of the page to update, with or without dashes.",
      "command": "string [update_properties|update_content|replace_content|apply_template|update_verification] - The update operation to perform.",
      "properties": "object - Required for update_properties command. A JSON map of property names to values.",
      "content_updates": "array [object {old_str, new_str, replace_all_matches}] - Required for update_content command. An array of search-and-replace operations.",
      "new_str": "string - Required for replace_content command. The new content string to replace the entire page content with.",
      "template_id": "string - Required for apply_template command. The ID of the template to apply.",
      "allow_deleting_content": "boolean - Optional. Set to true to allow deletion of child pages or databases during replace_content.",
      "icon": "string - Optional. An emoji, custom emoji name, or image URL. Use 'none' to remove.",
      "cover": "string - Optional. An external image URL. Use 'none' to remove.",
      "verification_status": "string [verified|unverified] - Optional for update_verification command.",
      "verification_expiry_days": "integer - Optional. Days until verification expires."
    }
  },
  {
    "name": "Notion:notion-update-view",
    "parameters": {
      "view_id": "string - The view to update. Accepts a view:// URI, a Notion URL with ?v= parameter, or a bare UUID.",
      "name": "string - Optional. New name for the view.",
      "configure": "string - Optional. View configuration DSL string. Supports FILTER, SORT BY, GROUP BY, CALENDAR BY, TIMELINE BY, MAP BY, CHART, FORM, SHOW, HIDE, COVER, WRAP CELLS, FREEZE COLUMNS, and CLEAR directives."
    }
  },
  {
    "name": "Notion:search",
    "parameters": {
      "query": "string - Semantic search query over your Notion workspace and connected sources.",
      "filters": "object {created_date_range, created_by_user_ids} - Optional filters to apply to search results. Only valid when query_type is 'internal'.",
      "query_type": "string [internal|user] - Optional. 'internal' for workspace search, 'user' to search for users by name or email.",
      "content_search_mode": "string [workspace_search|ai_search] - Optional. Override the default search mode.",
      "data_source_url": "string - Optional. URL of a data source to scope the search to.",
      "page_url": "string - Optional. URL or ID of a page to search within.",
      "teamspace_id": "string - Optional. ID of a teamspace to restrict results to.",
      "page_size": "integer - Optional. Maximum number of results to return (default 10, max 25).",
      "max_highlight_length": "integer - Optional. Maximum character length for result highlights (default 200). Set to 0 to omit."
    }
  }
]
```
