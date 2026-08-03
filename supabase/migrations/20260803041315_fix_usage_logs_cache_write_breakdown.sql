-- Include provider cache writes in the input-token accounting invariant.
-- Legacy aggregate rows may still omit the entire token breakdown.

alter table public.usage_logs
  drop constraint if exists usage_logs_input_token_breakdown_check;

alter table public.usage_logs
  add constraint usage_logs_input_token_breakdown_check
  check (
    (
      cached_input_tokens is null
      and cache_write_input_tokens is null
      and uncached_input_tokens is null
    )
    or (
      cached_input_tokens is not null
      and uncached_input_tokens is not null
      and input_tokens is not null
      and cached_input_tokens
        + coalesce(cache_write_input_tokens, 0)
        + uncached_input_tokens = input_tokens
    )
  )
  not valid;

alter table public.usage_logs
  validate constraint usage_logs_input_token_breakdown_check;
