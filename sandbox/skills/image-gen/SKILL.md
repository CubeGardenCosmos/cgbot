---
name: image-gen
description: Generate one image from a text prompt, optionally conditioned on up to 8 reference images under /workspace, through the generate_image MCP tool. Use when the user asks to create, draw, edit or restyle an image.
---

# image-gen

Use the `generate_image` MCP tool. It calls the bot bridge; the bot holds the provider key and
the sandbox never sees it.

## Arguments

- `prompt` (required): 1-8000 characters, not blank.
- `size` (optional): `WIDTHxHEIGHT`, each edge a multiple of 16 between 16 and 3840, aspect ratio
  at most 3:1. Omit it to match the first reference image, or 1024x1024 when there is none.
- `name` (optional): output file stem, 1-40 characters of `a-z 0-9 _ -`. Omit it for a generated
  name. Never reuse a name; the tool refuses an existing one.
- `reference_images` (optional): up to 8 absolute paths under `/workspace/`, each 1-512
  characters. Pass only paths the user gave you or files you created. Each file must be a PNG,
  JPEG or WebP image of at most 20 MiB. Symlinks, `..` components and hard-linked files are
  rejected.

## Rules

- Generate one image per task. Do not loop to get variants unless the user asks for them.
- Never retry after `outcome_unknown`. The provider may already have billed the request. Tell the
  user the generation outcome is unknown and ask before trying again.
- Retry only after `busy` or `too_many`, and wait for the other generation to finish first.
- Do not invent reference paths. If a path is missing or unreadable, ask the user for the file.

## Success

The tool returns `path` (under `/workspace/artifacts/imagegen/`), `width`, `height`, `bytes` and
`size`. Before you report success, verify the file: `test -s <path>` must pass, and the dimensions
must match the requested size. Then deliver it to the user with the `report_progress` tool, passing
`files=[path]`.

## Errors

The tool returns `{error, message}`. Codes:

- `invalid_request` (400): fix the arguments. The message says which one.
- `busy` (409): another image is still being generated for this user. Wait, then retry.
- `too_many` (429): the bot is at its global generation limit. Wait, then retry.
- `not_configured` (503): image generation is off on this deployment. Tell the user.
- `generation_failed` (502): the provider did not generate an image. Report the message. A retry
  is allowed.
- `outcome_unknown` (502): see the rules above. Never retry automatically.
- `storage_error` (500): the image could not be saved. Report it; do not retry without asking.
