-- Run once in Supabase SQL Editor if snow-gallery bucket already exists.
-- Raises the per-photo upload limit from 2 MB to 8 MB.
update storage.buckets
set file_size_limit = 8000000,
    allowed_mime_types = array['image/png', 'image/jpeg'],
    public = false
where id = 'snow-gallery';
