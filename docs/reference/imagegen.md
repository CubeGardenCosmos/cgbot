# M5 imagegen

Image generation for the coding sandbox. Code: `src/cgbot/coding/imagegen.py`. Skill:
`sandbox/skills/image-gen/SKILL.md`.

## Config

`[coding.imagegen]` in the coding TOML. Absent means image generation is off, and `POST
/bridge/imagegen` returns 503 `not_configured`. Fields: `base_url`, `api_key` (a `secret:` ref),
`model`, `mode` (`chat` or `images`), `timeout_seconds`, `proxy`, `max_concurrency`.

## Route

`POST /bridge/imagegen`, with `Authorization: Bearer <sandbox token>`. The user id comes from the
token (`SandboxAuth.resolve`), never from the body. Body: `prompt` (required), `size`, `name`,
`reference_images` (paths under `/workspace/`). Success returns `{path, width, height, bytes,
size}`. Errors return `{error, message}`.

| status | error | meaning |
|---|---|---|
| 400 | `invalid_request` | bad body or arguments; nothing was sent to the provider |
| 401 | `unauthorized` | missing or unknown bearer |
| 409 | `busy` | this user already has a generation in flight |
| 429 | `too_many` | global `max_concurrency` reached |
| 500 | `storage_error` | image could not be stored |
| 502 | `generation_failed` | provider rejected or returned no image; the request was not processed; retry allowed |
| 502 | `outcome_unknown` | timeout or dropped connection after sending; the provider may have billed it; do not retry automatically |
| 503 | `not_configured` | `[coding.imagegen]` absent |

## Storage

Output: `<host_data_dir>/users/<user>/artifacts/imagegen/<stem>.png` (sandbox path
`/workspace/artifacts/imagegen/<stem>.png`), mode 0644, owned 1000:1000. Each success appends one
JSON line to `index.jsonl` (`created_at, path, sha256, prompt, reference_sha256, model, size`).

## Not done here

- Upload handling (`store_input`, `/api/inputs`) is not part of this module.
- Real provider, real `chown` (needs root), and real Docker bridge integration are unverified by
  the offline tests.
