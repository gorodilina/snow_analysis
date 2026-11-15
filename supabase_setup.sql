-- Paste into the Supabase SQL Editor ONCE. This does not replace any app data.
-- All reads/writes are performed by the Streamlit Python server using a
-- Supabase SECRET key (or legacy service_role key). Never send that key to JS.
create table if not exists public.snow_sessions (
  token text primary key,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint snow_session_token_length check (char_length(token) between 32 and 90)
);
alter table public.snow_sessions enable row level security;
-- No browser (anon/authenticated) policies: public direct access is disabled.
revoke all on table public.snow_sessions from anon, authenticated;
create index if not exists snow_sessions_updated_at_idx
  on public.snow_sessions (updated_at);
-- Note: a link containing the token acts like a password.
-- Anyone holding that link can read and edit the session via the app.
