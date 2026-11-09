# Configuration

Everything is set with environment variables; there are no config files.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `APP_PASSWORD` | *(required)* | Password for the single `user` account. |
| `BASE_URL` | `http://localhost:8000` | The address you open the app at, e.g. `https://images.example.com`. Image links are built from it, and logins, uploads and other form submissions a browser sends from any other address are refused. Trailing slash is stripped. |
| `SITE_NAME` | `img-to-url` | Name shown in the browser tab title, top bar and footer. |
| `IMG_MAX_SIZE` | `10` | Maximum upload size in **MB**. |
| `IMG_EXPIRATION_HOURS` | `0` (disabled) | Images older than this many hours are deleted by the daily 05:00 cleanup job. |
| `IMG_CACHE_MINUTES` | `0` (no caching) | How long browsers may cache a served image, in whole minutes (e.g. `5` or `30`; sent as `Cache-Control: public, max-age=…`). `0` sends `Cache-Control: no-store`: nothing is cached, so a deleted image disappears on the next load. |
| `WORKERS` | `1` | uvicorn worker processes. One is enough for this app; each extra worker adds about 40 MB of memory and more startup CPU, so avoid raising it on small VMs. |
| `PORT` | `8000` | Port uvicorn listens on inside the container. |
| `DATA_DIR` | `/data` in Docker, `data` otherwise | Directory for the SQLite database and image files. In Docker, leave it alone and mount your storage at `/data` instead. |
| `SESSION_SECRET` | *(random, generated on first start)* | HMAC key for session cookies. When unset, a random key is created on first start, stored in the database and combined with `APP_PASSWORD`, so changing the password still logs every session out. Only set it if you want sessions to survive a password change. |
| `TZ` | `UTC` | Timezone used for the 05:00 cleanup schedule and log timestamps (the admin panel always shows UTC). |

## Storage

The database and the image files live in `/data` inside the container, so
mount a volume or a host directory **there**. A volume mounted at any other
path stays empty, and your data ends up in an anonymous volume that a new
container won't reuse.

The app runs as **UID 61001 and GID 61001**, not root. That number is chosen
so it doesn't match a real account on your host.

**Named volume**: nothing to prepare. Docker gives a new volume the right
owner on first use.

```bash
docker run ... -v img-to-url-data:/data img-to-url:latest
```

**Host directory (bind mount)**: create the directory and give it to 61001
**before** the first start. Otherwise Docker creates it owned by root and the
app can't write to it. No user or group with that number needs to exist on the
host.

```bash
sudo mkdir -p /srv/img-to-url
sudo chown 61001:61001 /srv/img-to-url
docker run ... -v /srv/img-to-url:/data img-to-url:latest
```

With rootless Docker or `userns-remap`, the host sees container IDs shifted
to different numbers, so a host `chown 61001` gives the wrong owner. Skip the
two `sudo` commands and use a path that doesn't exist yet. Docker creates it,
and this command gives it to 61001 from inside a container:

```bash
docker run --rm --user 0 -v /srv/img-to-url:/data img-to-url:latest chown 61001:61001 /data
```

## Behind a reverse proxy

- `BASE_URL` only affects the links shown and copied on the site; it does not
  change what the app listens on. Set it to the address visitors actually
  reach, such as your reverse proxy's domain.
- Serve the site over HTTPS so the password isn't sent in cleartext. With an
  `https://` `BASE_URL`, the session cookie is marked `Secure`.
- With **nginx**, raise `client_max_body_size` (default: 1 MB) to at least
  `IMG_MAX_SIZE`, or larger uploads fail with HTTP 413 before they reach the
  app.
- The app doesn't rate-limit `/login`, so throttle it at the proxy. See
  [Security](security.md).

## Troubleshooting

### Login says "This site's address is set to …"

The page was opened at a different address than `BASE_URL`, for example
`http://server-ip:8000` while `BASE_URL` is `https://images.example.com`. The
app only accepts logins, uploads and other form submissions from the
`BASE_URL` address, so that other sites can't forge them. Scheme, host and
port all have to match: `http://localhost:8000` and `http://127.0.0.1:8000`
are different addresses. Open the app at `BASE_URL`, or set `BASE_URL` to the
address you use, such as `http://server-ip:8000` while testing without a
reverse proxy.

### Permission errors on `/data`

The directory mounted at `/data` isn't owned by UID 61001. Give it to 61001 as
shown under [Storage](#storage).
