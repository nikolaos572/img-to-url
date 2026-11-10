# Security

img-to-url is built for one trusted user behind an HTTPS reverse proxy. This
page covers what you need to set up yourself and what the app already does.

## What you need to do

- **Pick a long password.** There is no built-in rate limiting or brute-force
  protection on `/login`, and the password is the only credential. Let your
  reverse proxy (fail2ban, Caddy or nginx limits, etc.) throttle login
  attempts.
- **Use HTTPS.** Put an HTTPS reverse proxy in front of the app so the
  password isn't sent in cleartext. See
  [Configuration](configuration.md#behind-a-reverse-proxy).

## What the app does

- Uploads are limited to image formats (`png jpg jpeg gif webp bmp avif ico`)
  by extension **and** magic-byte checks. **SVG is deliberately not allowed**:
  serving uploaded SVG inline would let scripts run on the public image URL.
- **Every upload is re-encoded**, so only the picture is stored and served.
  The file is decoded (by Pillow, limited to the formats above) and written
  out as a new file of the same format. That drops all metadata: EXIF with
  GPS position, device make, model and serial number, capture time, XMP,
  IPTC, embedded thumbnails, comments, PNG text chunks and ICC profiles. Any
  data hidden in or after the image data goes too. EXIF orientation is
  applied to the pixels and ICC colors are converted to sRGB, so pictures
  look the same; transparency and animation frames, timing and looping are
  kept. A file that doesn't decode is refused, and the stored file's format,
  extension and `Content-Type` always match its content. JPEG, lossy WebP
  and AVIF are compressed again (JPEG with its own quantization tables), so
  their bytes change but not visibly.
- Images that decode to more than 50 million pixels (all animation frames
  together) are refused before decoding, so a small file can't make the
  server allocate gigabytes.
- Every HTML page sends a strict **`Content-Security-Policy`**
  (`default-src 'self'; img-src 'self' data: blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`).
  All JavaScript and CSS load only from the site's own `/static` files, and
  there are no inline scripts or inline event handlers anywhere, so if a
  script injection ever slipped through, the browser would refuse to run it.
- Served images carry their own policy,
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`,
  plus `X-Content-Type-Options: nosniff`. Whatever a crafted file might
  contain, opening its link directly can't run scripts or load anything,
  and the sandbox gives it a unique origin with no access to the site's
  cookies or pages. Inline styles are allowed only because Chromium's
  image viewer needs them. Embedding images on other sites isn't affected.
- Request bodies are capped before anything parses them: uploads at
  `IMG_MAX_SIZE` plus a little multipart overhead, every other form at 64 KB.
  `/upload` also checks the session *before* reading the body, so anonymous
  clients can't make the server buffer or spool data to disk.
- Session cookies are `HttpOnly`, `SameSite=Lax`, `Secure` when `BASE_URL` is
  https, and signed with HMAC-SHA256, so every worker validates them without
  a session store. The signing key includes a random per-install secret, so a
  leaked cookie can't be used to guess the password offline.
- **Logging out ends the session on the server**, not just in your browser:
  the cookie stops working, including any copy of it taken earlier. Changing
  `APP_PASSWORD` ends every session at once.
- All state-changing endpoints are POST, and a POST whose `Origin` header
  isn't exactly `BASE_URL`'s scheme, host and port is refused (403) before
  anything else happens. That stops forged submissions from every other
  site, including sibling subdomains such as `blog.example.com`, which
  `SameSite=Lax` cookies alone let through. Browsers always send `Origin` on
  POST; requests without it (curl, scripts) can't be forged by a web page,
  so they're allowed and still need a valid session cookie.
- The container runs as an unprivileged user, UID/GID 61001, not root. The
  number is above the range Linux gives to real accounts (up to 60000), so it
  won't match a host login. That matters if someone ever breaks out of the
  container: they'd land as an ID that owns nothing on the host, instead of
  something like uid 1000, which often has passwordless sudo.

## Things to know

- The logs show the address each request came from. Behind a reverse proxy
  that is the proxy's address, unless `FORWARDED_ALLOW_IPS` names the proxy:
  then the visitor's address from `X-Forwarded-For` is logged. List only
  your proxy there, since any address you trust can put whatever it likes in
  the logs. See [Configuration](configuration.md#behind-a-reverse-proxy).
- Direct image links are public by design (that's the point of the tool).
  Only uploading and the admin panel require a login.
- By default, served images aren't cached at all (`Cache-Control: no-store`),
  so a deleted image is gone for everyone on their next load. If you set
  `IMG_CACHE_MINUTES`, a deleted image may stay visible in a viewer's browser
  cache for up to that long; the server itself stops serving it immediately
  either way. Chat apps that proxy images, such as Discord or Slack, keep their
  own copies regardless.
