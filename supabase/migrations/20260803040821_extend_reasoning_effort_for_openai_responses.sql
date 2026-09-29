-- Add OpenAI Responses reasoning levels to the V1 user preference validator.
-- Existing DeepSeek values remain valid; runtime provider validation rejects
-- incompatible provider/effort combinations.

create or replace function private.is_valid_llm_preferences_v1(value jsonb)
returns boolean
language plpgsql
immutable
security invoker
set search_path = pg_catalog
as $function$
begin
  if value is null then
    return true;
  end if;
  if jsonb_typeof(value) <> 'object'
     or value - array['model', 'reasoning_effort']::text[] <> '{}'::jsonb then
    return false;
  end if;
  if value ? 'model' and value -> 'model' is distinct from 'null'::jsonb then
    if jsonb_typeof(value -> 'model') <> 'string'
       or substring(value ->> 'model' from 1 for 1) ~ '[[:space:]]'
       or right(value ->> 'model', 1) ~ '[[:space:]]'
       or length(value ->> 'model') not between 1 and 100 then
      return false;
    end if;
  end if;
  if value ? 'reasoning_effort'
     and value -> 'reasoning_effort' is distinct from 'null'::jsonb then
    if jsonb_typeof(value -> 'reasoning_effort') <> 'string'
       or value ->> 'reasoning_effort' not in (
         'off', 'none', 'low', 'medium', 'high', 'xhigh', 'max'
       ) then
      return false;
    end if;
  end if;
  return true;
exception when others then
  return false;
end;
$function$;
