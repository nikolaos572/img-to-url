# Development and testing

Requires Python 3.12 or newer.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -e ".[dev]"
APP_PASSWORD=dev uvicorn app.main:app --reload
```

`requirements.txt` holds the exact versions the Docker image uses, so
installing it alongside the project runs your code and the tests against
the same packages that ship.

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

## Updating dependencies

`pyproject.toml` lists the allowed version ranges. `requirements.txt` pins
the exact version of every package, indirect ones included, and the image
installs only that list (the build fails if it is incomplete). To move to
newer versions, change a range in `pyproject.toml` if needed and regenerate
the list inside the same Python image the Dockerfile uses:

```bash
docker run --rm -v "$PWD":/src:ro python:3.12-slim sh -c '
  mkdir /tmp/p && cp -r /src/pyproject.toml /src/app /tmp/p &&
  pip install -q --disable-pip-version-check --root-user-action=ignore /tmp/p &&
  { grep "^#" /src/requirements.txt; pip freeze --exclude img-to-url; }' > requirements.new
mv requirements.new requirements.txt
```

Then reinstall (`pip install -r requirements.txt -e ".[dev]"`) and run both
test suites, Docker included, before committing the new list.

## Things to keep in mind

- **No inline JavaScript or CSS.** The Content-Security-Policy blocks inline
  scripts, inline event handlers and `style` attributes. Put scripts and styles
  in `app/static/` and wire up events from there.
- **Code must work with several workers.** `WORKERS` can be above 1, so keep
  shared state in SQLite, not in process memory. For example, the daily
  cleanup claims each day's run in the database so only one worker does it.
