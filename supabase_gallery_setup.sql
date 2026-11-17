-- Run once in the existing Supabase project, after supabase_setup.sql.
-- Private bucket for rendered map screenshots. Only the Streamlit server's
-- secret key can read/write images; no public or anonymous policies.
insert into storage.buckets (
  id, name, public, file_size_limit, allowed_mime_types
) values (
  'snow-gallery', 'snow-gallery', false, 2000000,
  array['image/png', 'image/jpeg']
)
on conflict (id) do update set
  public = false,
  file_size_limit = 2000000,
  allowed_mime_types = array['image/png', 'image/jpeg'];
