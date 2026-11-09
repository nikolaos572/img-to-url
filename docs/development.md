# Development and testing

Requires Python 3.12 or newer.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
APP_PASSWORD=dev uvicorn app.main:app --reload
```

Open `http://localhost:8000/login` and log in as `user` / `dev`. The database
and images go to `./data`, which git ignores. Every setting is an environment
variable; see [Configuration](configuration.md).

## Tests

```bash
python -m pytest tests/ -v -m "not docker"           # unit + integration (no Docker needed)
python -m pytest tests/test_docker.py -v -m docker   # Docker smoke tests (needs Docker)
```

The Docker smoke tests build the image, run it with `WORKERS=4` and drive the
full flow (login, upload, public link, admin, delete, clear all) over real
HTTP.

## Things to keep in mind

- **No inline JavaScript or CSS.** The Content-Security-Policy blocks inline
  scripts, inline event handlers and `style` attributes. Put scripts and styles
  in `app/static/` and wire up events from there.
- **Code must work with several workers.** `WORKERS` can be above 1, so keep
  shared state in SQLite, not in process memory. For example, the daily
  cleanup claims each day's run in the database so only one worker does it.
