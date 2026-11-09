# AGENTS.md

Guidance for anyone changing this project, people and AI coding agents alike.
These are defaults, not hard rules. If a task needs a different approach, take
it, explain why, and update this file and the docs to match. When this file and
the code disagree, the code wins, and this file should be fixed.

## The project

img-to-url is a small self-hosted app: paste or upload an image, get a direct
link to share. It runs as a single Docker container.

- What it does and how to run it: `README.md`
- Settings, storage and deployment: `docs/configuration.md`
- Security model: `docs/security.md`
- Local setup and running the tests: `docs/development.md`

## Working on a task

- If a request is unclear or could mean more than one thing, ask before writing
  code. Don't guess at requirements.
- State the assumptions you make.
- Keep changes focused on what was asked. If you notice something else worth
  fixing, mention it rather than quietly changing it.
- For bigger changes, such as new dependencies, a different data or config
  format, or restructuring, describe the plan and get agreement first.
- Ask before anything destructive: deleting data, force-pushing, rewriting
  history.
- Prefer the simplest solution that works. This is a small app; keep it small.

## Code

- Follow the patterns and style already in the codebase.
- Avoid duplicate code: reuse or extend existing helpers before writing new ones.
- Comments explain why, not what. Remove dead code instead of commenting it out.
- Add a dependency only when it clearly earns its place, and say why.
- Fail with clear error messages, especially for configuration mistakes.
- UI changes should work in light and dark mode, on phone-sized screens and
  with the keyboard.

## Tests

- New features and bug fixes come with tests. For a bug, where practical, first
  write a test that fails because of it.
- Run the full test suite before calling a change done. Fix failures; don't
  skip or weaken tests to make them pass.
- If you change how the container is built or run, run the Docker tests too.
- Never claim something works or tests pass without running them. If you
  couldn't run something, say so.

## Documentation

Docs are part of the change, not a follow-up.

- Added, renamed or removed an environment variable: update the table in
  `docs/configuration.md`, and the README example if it's commonly used.
- Changed behavior users can see: update `README.md` and the relevant docs page.
- Touched authentication, uploads, headers or permissions: make sure
  `docs/security.md` is still true.
- Changed setup, commands or tests: update `docs/development.md`.
- Visibly changed the UI: retake the README screenshot.
- Write for someone installing from scratch. Keep it short and concrete.

## Security and privacy

- Treat every request, upload and header as untrusted input.
- Don't weaken existing protections, such as the Content-Security-Policy, the
  upload checks or the non-root container, without discussing it first.
- New settings should default to the safe option.
- Never commit secrets, real passwords, personal data or real domains. Use
  example.com and obvious placeholders in docs, tests and screenshots.

## Git

- Don't commit or push directly to `main` unless the user explicitly asks for
  it. Work on a branch and open a pull request instead.
- AI agents: commit or push only when asked.
- Small, focused commits whose message says what changed and why.
- Call out breaking changes (configuration, data layout, file ownership) in the
  commit message.
- Don't commit generated files, local data, virtual environments or build output.
- Keep personal information, internal hostnames and links to private chat
  sessions out of commits and PR descriptions.
