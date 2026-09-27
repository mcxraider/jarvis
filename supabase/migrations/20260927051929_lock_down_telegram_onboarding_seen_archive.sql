-- The private archive is intentionally unreadable to every runtime role. An
-- explicit deny policy preserves PostgreSQL's default-deny behavior while
-- making the security posture visible to Supabase's RLS advisor.
create policy telegram_onboarding_seen_archive_deny_all
on private.telegram_onboarding_seen_archive
for all
to public
using (false)
with check (false);
