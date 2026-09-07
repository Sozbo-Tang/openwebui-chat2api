#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openwebui-chat2api — expose any Open WebUI instance as a local OpenAI-compatible API.

How it works:
  1. Open WebUI keeps its session token in `localStorage` on the site's origin.
  2. On first run this script opens a visible Chrome window (Playwright, dedicated
     profile) where you sign in once (SSO/username/password). The token is saved
     to `token.json` and reused afterwards — no browser needed for later runs.
  3. A local HTTP server forwards OpenAI-style requests to the instance's native
     `/api/chat/completions` endpoint. Streaming (SSE) and non-streaming are
     both supported, as are Open WebUI API keys (long-lived, preferred over the
     expiring JWT).

Usage:
  python3 chat2api.py --login               # first run: sign in via Chrome
  python3 chat2api.py --list-models         # list available models
  python3 chat2api.py                       # start the API server (127.0.0.1:8000)

  # Or provide a token directly (browser DevTools -> Application -> Local Storage)
  python3 chat2api.py --token "eyJ..." --no-browser

OpenAI-compatible endpoints:
  GET  /v1/models             list models
  POST /v1/chat/completions   chat completions (stream=true returns SSE)
  GET  /v1/version            version info

Thinking-level variants:
  For reasoning models served behind vLLM that honour the OpenAI
  `reasoning_effort` parameter, you can register "virtual models" that bake a
  fixed thinking level into the request, e.g.:

    python3 chat2api.py --effort-model GLM-5.2-NVFP4

  This exposes GLM-5.2-NVFP4-Fast / -Low / -Medium / -High alongside the base
  model, so any OpenAI client can pick a thinking level by picking a model.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# Default Open WebUI endpoint. Override with --base-url.
BASE_URL = "http://localhost:3000"

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.json")          # NEVER commit this file
PROFILE_DIR = os.path.join(HERE, ".chrome-profile")    # browser profile with login state

# Base models that get thinking-level virtual variants (see docstring).
# You can also add them on the command line with --effort-model.
EFFORT_MODELS: list[str] = []

# Suffix -> OpenAI `reasoning_effort` value. The backend must support the
# parameter (vLLM does); otherwise requests still go through unchanged.
EFFORT_LEVELS = {
    "Fast": "none",     # disable thinking, fastest
    "Low": "low",
    "Medium": "medium",
    "High": "high",
}

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


class TokenStore:
    """Persist/load the Open WebUI session token."""

    def __init__(self, path: str, url: str):
        self.path = path
        self.url = url

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("url") != self.url:
                return None
            return data
        except Exception:
            return None

    def save(self, token: str, api_key: str = None):
        data = {"url": self.url, "token": token, "api_key": api_key,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(self.path, 0o600)
        return data


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def browser_login(base_url: str, profile_dir: str, timeout_s: int) -> str:
    """Open a visible Chrome window, wait for the user to sign in, then read
    the Open WebUI token from localStorage.

    Caveat: localStorage may still hold a token that the backend already
    invalidated (the frontend clears it a beat later). Any token found here is
    verified against /api/models; stale ones are removed so a real sign-in
    can proceed."""
    from playwright.sync_api import sync_playwright  # lazy import

    chrome = find_chrome()
    print("=" * 60)
    print(f"Opening Chrome: {base_url}")
    print("Sign in in the window that pops up (skipped if already logged in).")
    print(f"Waiting up to {timeout_s}s for the session token...")
    print("=" * 60)

    with sync_playwright() as p:
        if chrome:
            ctx = p.chromium.launch_persistent_context(
                profile_dir, headless=False, executable_path=chrome)
        else:
            print("System Chrome not found, using Playwright Chromium "
                  "(run `playwright install chromium` first)")
            ctx = p.chromium.launch_persistent_context(profile_dir, headless=False)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[warn] Failed to load page (may still be redirecting): {e}")

        deadline = time.time() + timeout_s
        warned_stale = False
        while time.time() < deadline:
            try:
                token = page.evaluate("localStorage.getItem('token')")
                if token:
                    # Verify before accepting, otherwise a stale token that the
                    # frontend has not cleared yet gets mistaken for a login.
                    status = page.evaluate(
                        """async t => {
                            const r = await fetch('/api/models',
                                {headers: {Authorization: 'Bearer ' + t}});
                            return r.status;
                        }""", token)
                    if status == 200:
                        print("\nSigned in, token verified.")
                        return token
                    if not warned_stale:
                        warned_stale = True
                        print("\n[warn] Stale token found in localStorage, "
                              "clearing it and waiting for a real sign-in...")
                    page.evaluate("localStorage.removeItem('token')")
            except Exception:
                pass
            page.wait_for_timeout(1000)

        raise RuntimeError(
            f"Timed out after {timeout_s}s. Run --login again and complete "
            "the sign-in in the Chrome window."
        )


def fetch_api_key(base_url: str, token: str) -> str | None:
    """Best-effort: fetch the Open WebUI API key (long-lived, replaces the JWT).
    Returns None if the instance does not expose it."""
    for method in ("GET", "POST"):
        try:
            r = requests.request(
                method, f"{base_url}/api/v1/auths/api_key",
                headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if r.status_code == 200:
                key = r.json().get("api_key")
                if key:
                    print(f"Got Open WebUI API key: {key[:12]}...")
                    return key
        except Exception:
            pass
    return None


def authenticate(args) -> dict:
    """Return {'token': ..., 'api_key': ...}, opening the browser if needed."""
    store = TokenStore(TOKEN_FILE, args.base_url)
    saved = store.load()

    if args.token:
        return {"token": args.token, "api_key": None}

    if saved and (saved.get("api_key") or saved.get("token")):
        # Validate at startup so we never boot into a 401 retry loop.
        bearer = saved.get("api_key") or saved.get("token")
        try:
            r = requests.get(f"{args.base_url}/api/models",
                             headers={"Authorization": f"Bearer {bearer}"}, timeout=15)
            if r.status_code == 200:
                return {"token": saved.get("token"), "api_key": saved.get("api_key")}
            print(f"[chat2api] Saved credentials rejected (HTTP {r.status_code}), "
                  "need to sign in again...")
        except Exception as e:
            print(f"[chat2api] Failed to validate saved credentials: {e}")

    token = browser_login(args.base_url, args.profile, args.login_timeout)
    api_key = fetch_api_key(args.base_url, token) if args.use_api_key else None
    store.save(token, api_key)
    return {"token": token, "api_key": api_key}


def resolve_effort(requested: str):
    """Map a virtual model id (<base>-<Suffix>) to (base_model, effort|None)."""
    for base in EFFORT_MODELS:
        if requested == base:
            return base, None
        prefix = base + "-"
        if requested.startswith(prefix):
            suffix = requested[len(prefix):]
            if suffix in EFFORT_LEVELS:
                return base, EFFORT_LEVELS[suffix]
    return None, None


# ---------------------------------------------------------------- HTTP server

class Proxy:
    """Holds credentials and forwards requests to the Open WebUI backend."""

    def __init__(self, creds: dict, args):
        self.creds = creds
        self.base_url = args.base_url
        self.models_cache = {"at": 0, "data": None}
        self.args = args

    def bearer(self) -> str:
        # Prefer the API key (does not expire), fall back to the JWT.
        return self.creds.get("api_key") or self.creds["token"]

    def refresh(self) -> bool:
        """Re-run the browser login when the token is rejected."""
        if self.args.no_browser:
            return False
        print("\n[chat2api] Token rejected, trying to re-authenticate...")
        try:
            token = browser_login(self.base_url, self.args.profile, self.args.login_timeout)
        except Exception as e:
            print(f"[chat2api] Re-login failed: {e}")
            return False
        api_key = fetch_api_key(self.base_url, token) if self.args.use_api_key else None
        TokenStore(TOKEN_FILE, self.base_url).save(token, api_key)
        self.creds = {"token": token, "api_key": api_key}
        return True

    def call(self, method: str, path: str, stream: bool = False, **kwargs):
        """Request with a single 401 -> re-auth retry. For streaming endpoints
        you MUST pass stream=True, otherwise requests buffers the whole SSE
        body until the upstream closes the connection."""
        for attempt in (0, 1):
            r = requests.request(
                method, f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.bearer()}",
                         **kwargs.pop("headers", {})},
                timeout=kwargs.pop("timeout", 600), stream=stream, **kwargs)
            if r.status_code == 401 and attempt == 0 and self.refresh():
                continue
            return r
        return r

    def list_models(self, force=False):
        now = time.time()
        if self.models_cache["data"] is None or force or now - self.models_cache["at"] > 60:
            r = self.call("GET", "/api/models")
            if r.status_code == 200:
                self.models_cache = {"at": now, "data": r.json()}
        data = self.models_cache["data"]
        if data is None:
            return None
        data = dict(data)
        data["data"] = list(data.get("data", []))
        # Append thinking-level virtual models for each registered base model.
        for base in EFFORT_MODELS:
            src = next((m for m in data["data"] if m.get("id") == base), None)
            for suffix in EFFORT_LEVELS:
                vid = f"{base}-{suffix}"
                if any(m.get("id") == vid for m in data["data"]):
                    continue
                m = dict(src) if src else {}
                m["id"] = vid
                m["name"] = vid
                data["data"].append(m)
        return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, detail=None):
        self._json(code, {"error": {"message": message,
                                    "type": "chat2api_error",
                                    "detail": detail}})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy

        if path in ("/", "/health"):
            models = proxy.list_models()
            n = len(models.get("data", [])) if models else 0
            self._json(200, {"status": "ok", "backend": proxy.base_url,
                             "models": n, "docs": "POST /v1/chat/completions"})
        elif path == "/v1/version":
            r = requests.get(f"{proxy.base_url}/api/version", timeout=15)
            self._json(200, {"openwebui": r.json() if r.status_code == 200 else None,
                             "chat2api": "1.0.0"})
        elif path in ("/v1/models", "/api/models"):
            models = proxy.list_models(force=True)
            if models is None:
                self._error(502, "Failed to fetch model list (auth may have expired)")
                return
            self._json(200, models)
        elif path.startswith("/api/"):
            # Read-only passthrough for other backend endpoints.
            r = proxy.call("GET", path)
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}")

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        proxy: Proxy = self.server.proxy

        if path in ("/v1/chat/completions", "/api/chat/completions"):
            self._chat()
        elif path.startswith("/api/"):
            r = proxy.call("POST", path, json=self._read_body())
            self._passthrough(r)
        else:
            self._error(404, f"Unknown path: {path}")

    def _chat(self):
        proxy: Proxy = self.server.proxy
        body = self._read_body()
        if not body.get("messages"):
            self._error(400, "Request body is missing the messages field")
            return

        # Virtual model -> real model + reasoning effort.
        base, effort = resolve_effort(body.get("model") or "")
        if base and effort is not None:
            body = dict(body)
            body["model"] = base
            body["reasoning_effort"] = effort

        stream = bool(body.get("stream"))
        r = proxy.call("POST", "/api/chat/completions", json=body, stream=stream)
        if r.status_code != 200:
            r.close()
            self._error(502, "Upstream request failed",
                        f"HTTP {r.status_code}: {r.text[:300]}")
            return

        if not stream:
            self._json(200, r.json())
            r.close()
            return

        # Streaming: forward SSE lines as-is. Two gotchas that break strict
        # clients (e.g. the eventsource-parser used by several agents):
        #   1. SSE events MUST be separated by a blank line (\n\n); otherwise
        #      the parser accumulates every `data:` line into one event and the
        #      whole stream fails to parse (silent "empty response").
        #   2. Some upstreams keep the connection alive after [DONE], so we stop
        #      reading at [DONE] and close the downstream connection to give the
        #      client a prompt end-of-stream.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for raw in r.iter_lines(decode_unicode=True):
                if raw:
                    self.wfile.write((raw + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    if raw.strip() == "data: [DONE]":
                        break
        except (BrokenPipeError, ConnectionResetError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError):
            pass  # client disconnected or upstream stream broke
        finally:
            r.close()
            self.close_connection = True

    def _passthrough(self, r: requests.Response):
        try:
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() in ("content-type", "content-length", "cache-control"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(r.content)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description="Open WebUI -> local OpenAI-compatible API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--base-url", default=BASE_URL, help="Open WebUI instance URL")
    ap.add_argument("--token", help="Use this JWT directly instead of browser login")
    ap.add_argument("--no-browser", action="store_true",
                    help="Never open the browser (use with --token; no re-login on 401)")
    ap.add_argument("--profile", default=PROFILE_DIR, help="Chrome user-data directory")
    ap.add_argument("--login-timeout", type=int, default=180,
                    help="Seconds to wait for manual sign-in")
    ap.add_argument("--use-api-key", action="store_true",
                    help="Fetch the long-lived Open WebUI API key after login")
    ap.add_argument("--effort-model", action="append", default=[],
                    help="Register thinking-level variants for this model "
                         "(repeatable; e.g. --effort-model GLM-5.2-NVFP4)")
    ap.add_argument("--login", action="store_true",
                    help="Sign in and save the token, then exit")
    ap.add_argument("--list-models", action="store_true",
                    help="List models and exit")
    args = ap.parse_args()
    if args.no_browser and not args.token:
        ap.error("--no-browser requires --token")

    EFFORT_MODELS.extend(args.effort_model)

    creds = authenticate(args)

    if args.login:
        print(f"Signed in, token saved to {TOKEN_FILE}")
        return

    proxy = Proxy(creds, args)

    if args.list_models:
        models = proxy.list_models(force=True)
        if models is None:
            print("Failed to fetch models (token may have expired):", file=sys.stderr)
            sys.exit(1)
        print("Available models:")
        for m in models.get("data", []):
            print(f"  - {m['id']}")
        return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.proxy = proxy
    models = proxy.list_models()
    n = len(models.get("data", [])) if models else 0
    print("-" * 60)
    print(f"chat2api listening on http://{args.host}:{args.port}")
    print(f"backend: {args.base_url}  |  models: {n}")
    print("  OpenAI-compatible endpoints:")
    print("    GET  /v1/models")
    print("    POST /v1/chat/completions   (stream=true supported)")
    print("  Ctrl+C to exit")
    print("-" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
