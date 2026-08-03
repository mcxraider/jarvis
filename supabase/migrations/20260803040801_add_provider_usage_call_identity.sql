-- Preserve exact provider/model/tier identity for each LLM call. Existing
-- aggregate rows remain valid and intentionally have NULL identity fields.

alter table public.usage_logs
  add column if not exists provider text,
  add column if not exists requested_model text,
  add column if not exists cache_write_input_tokens bigint,
  add column if not exists reasoning_tokens bigint,
  add column if not exists request_input_tokens bigint,
  add column if not exists pricing_tier text;

alter table public.usage_logs
  add constraint usage_logs_provider_check
  check (provider is null or provider in ('deepseek', 'openai'))
  not valid;

alter table public.usage_logs
  add constraint usage_logs_extended_token_counts_check
  check (
    (cache_write_input_tokens is null or cache_write_input_tokens >= 0)
    and (reasoning_tokens is null or reasoning_tokens >= 0)
    and (request_input_tokens is null or request_input_tokens >= 0)
  )
  not valid;

alter table public.usage_logs validate constraint usage_logs_provider_check;
alter table public.usage_logs validate constraint usage_logs_extended_token_counts_check;

comment on column public.usage_logs.provider is
  'Actual provider that served this LLM call; NULL for legacy aggregate rows.';
comment on column public.usage_logs.requested_model is
  'Provider-compatible model requested by Jarvis before provider alias resolution.';
comment on column public.usage_logs.model is
  'Actual model identifier returned by the provider for LLM call rows.';
comment on column public.usage_logs.cache_write_input_tokens is
  'Input tokens written to provider prompt cache when reported.';
comment on column public.usage_logs.reasoning_tokens is
  'Reasoning tokens included within billed output tokens; never billed twice.';
comment on column public.usage_logs.request_input_tokens is
  'Per-request input count used to derive context-sensitive pricing tiers.';
comment on column public.usage_logs.pricing_tier is
  'Verified pricing tier derived per request, such as standard or long_context.';
