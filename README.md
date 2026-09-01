# openwebui-chat2api

Expose **any [Open WebUI](https://github.com/open-webui/open-webui) instance** as a
local **OpenAI-compatible API** — so you can use it from Cherry Studio, ZCode,
Claude Code-style agents, or any OpenAI client, without publishing your
credentials anywhere.

The tricky part of talking to Open WebUI programmatically is authentication
(SSO / username-password / LDAP). This project solves it by reusing a real
browser session:

1. On first run it opens a visible Chrome window (Playwright, dedicated profile)
   where you sign in once.
2. The session token is read from `localStorage`, saved locally, and reused.
   No browser is needed for later runs; if the token expires it re-opens the
   browser automatically and signs you back in via the still-valid SSO session.
3. A local HTTP server forwards OpenAI-style requests to the instance's native
   `/api/chat/completions` endpoint — streaming (SSE) and non-streaming.

## Features

- 🔐 Browser-session auth for any login flow (SSO, OIDC, LDAP, ...)
- ♻️ Token persisted locally; auto re-login on 401
- 🔑 Optional long-lived Open WebUI API key (preferred over the expiring JWT)
- 📡 Streaming SSE passthrough with strict-event framing (works with
  `eventsource-parser`-based clients)
- 🧠 **Thinking-level variants** for reasoning models (see below)

## Requirements

- Python 3.10+
- `pip install requests playwright`
- Chrome/Chromium installed (or run `playwright install chromium`)

## Usage

```bash
# 1. First run: sign in via Chrome (once)
python3 chat2api.py --login --base-url https://chat.example.com

# 2. Start the API server
python3 chat2api.py --base-url https://chat.example.com
#    chat2api listening on http://127.0.0.1:8000

# 3. Point any OpenAI client at http://127.0.0.1:8000/v1
```

> If you prefer not to use the browser flow, grab the token from
> DevTools → Application → Local Storage (key `token`) and run:
> `python3 chat2api.py --token "eyJ..." --no-browser --base-url https://...`

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /v1/models` | Model list |
| `POST /v1/chat/completions` | Chat completions (`stream: true` → SSE) |
| `GET /v1/version` | Version info |

### Thinking-level variants

Reasoning models served behind vLLM honour the OpenAI `reasoning_effort`
parameter. Since most clients have no UI for it, you can register *virtual
models* that bake a fixed thinking level into the request:

```bash
python3 chat2api.py --effort-model GLM-5.2-NVFP4 --base-url https://chat.example.com
```

This exposes:

| Model id | Effect |
|---|---|
| `GLM-5.2-NVFP4` | default behaviour (thinking on) |
| `GLM-5.2-NVFP4-Fast` | `reasoning_effort: none` — thinking off, fastest |
| `GLM-5.2-NVFP4-Low` | `reasoning_effort: low` |
| `GLM-5.2-NVFP4-Medium` | `reasoning_effort: medium` |
| `GLM-5.2-NVFP4-High` | `reasoning_effort: high` |

Pick a "model" from the client's model picker to switch thinking level.
`--effort-model` is repeatable for multiple base models; the levels map is
configurable in `EFFORT_LEVELS` at the top of `chat2api.py`.

## Security notes

- `token.json` and `.chrome-profile/` contain real session credentials.
  Both are covered by `.gitignore` — never commit or share them.
- This is a single-user personal proxy: it forwards **your** account's quota.
  Check your instance's acceptable-use policy before running heavy workloads.
- The server binds to `127.0.0.1` by default and has no auth of its own;
  use `--host 0.0.0.0` only on trusted networks.

## How it works

```
OpenAI client ──► local HTTP server (this repo) ──► Open WebUI /api/chat/completions
                        │  Authorization: Bearer <token from browser session>
```

The upstream SSE stream is forwarded line-by-line with blank-line event
separators (required by strict `eventsource-parser` clients) and terminates
immediately after `data: [DONE]` (some upstreams keep the connection alive
otherwise, which makes strict clients hang waiting for end-of-stream).

## License

MIT
