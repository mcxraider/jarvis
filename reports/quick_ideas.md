# Ideas


### Reliability


### Safety


### Agent capability enhancements


### LLM

- Prompt caching: once the orchestrator has determined the tool call for an initial
  prompt, cache the prompt and tool-call output so matching requests can skip the
  decision layer.
- Tiered model routing: investigate how to estimate task difficulty and route each
  request to an appropriately capable model.

### Formatting (UI)


### Logging


### Tooling


### UX


### Speech


### User enhancements/ Integrations/ features

- GitHub issues: investigate whether Jarvis can read, create, and update issues in
  its GitHub repository.

### Telegram Bot

- Batch forwarded messages: when a forwarded message arrives, wait 5 seconds for
  additional forwarded messages, then combine the batch into a single formatted
  input (for example, three forwarded messages become one request).
