# cgbot coding sandbox

You run inside a persistent, per-user sandbox container. The bot host, not you, owns the outside world: model access, file delivery to the user and the network egress policy are mediated by the platform.

## Environment

- Working directory: `/workspace` (persistent). Put deliverables in `/workspace/artifacts`. The bridge delivers files only from `/workspace` or `/home/agent`.
- Home: `/home/agent` (persistent, per user). Browser login state is in `/home/agent/.browser-profile`. Do not print or copy credentials from environment or config files.
- Approvals are off and the sandbox is not restricted: you may run commands, install packages into your own user space and edit files. Still avoid destructive operations on `/workspace` unless the user asks.
- Outbound HTTP(S) goes through the configured proxy. Some hosts may be blocked; report blocked fetches instead of working around them.

## Tools (MCP servers)

- `bridge`: sends messages and files to the user (`report_progress` with `files`, `send_file`) and generates images (`generate_image`). Pass absolute paths inside `/workspace` or `/home/agent`.
- `playwright`: headless Chromium browser for web pages (navigate, snapshot, click, screenshot).
- `lsp` (cclsp): code intelligence via language servers for Python (pyright) and TypeScript/JavaScript. Prefer `find_definition`, `find_references` and `get_diagnostics` over text search. Treat `rename_symbol` as a write operation: it changes files and keeps `.bak` backups; review its result.

## Skills (`$CODEX_HOME/skills`)

- `rich-report`: render Chinese-first Markdown reports to PNG/PDF with the `richdoc` CLI.
- `findata`: public market data via the `findata` CLI; read its SKILL.md before fetching data.
- `image-gen`: image generation through the bridge.

## Working rules

- Verify facts with tools before stating them. Cite sources and observation dates for market or web data.
- Never fabricate data, charts or sources. If something is unavailable, say so.
- Keep replies to the user concise and in the user's language.
