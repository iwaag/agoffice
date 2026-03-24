import logging
logging.basicConfig(level=logging.DEBUG, force=True)
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from agoffice_worker.routers.chat_pro import (
    HandshakeLoggingASGIApp,
    socket_server as chat_socket_server,
)
from agoffice_worker.routers.tunnel import router as tunnel_router

app = FastAPI(
    title="agoffice-worker"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(tunnel_router, prefix="/tunnel", tags=["tunnel"])

combined_app = HandshakeLoggingASGIApp(chat_socket_server, other_asgi_app=app, socketio_path="chat/realtime")

ASYNCAPI_SPEC_PATH = Path(__file__).resolve().parent / "asyncapi" / "realtime_chat.yaml"



@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logging.error(f"caught exception: {exc.detail}", exc_info=True)
    return exc
    
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/asyncapi.yaml", include_in_schema=False)
async def asyncapi_spec() -> Response:
    return Response(
        content=ASYNCAPI_SPEC_PATH.read_text(encoding="utf-8"),
        media_type="application/yaml",
    )


@app.get("/asyncapi", include_in_schema=False)
async def asyncapi_docs() -> HTMLResponse:
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agoffice-worker AsyncAPI</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f1e8;
      --panel: #fffdf8;
      --ink: #1f2933;
      --muted: #52606d;
      --accent: #0b6e4f;
      --border: #d9d1c3;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
      background:
        radial-gradient(circle at top right, rgba(11, 110, 79, 0.12), transparent 34%),
        linear-gradient(180deg, #fbf8f1 0%, var(--bg) 100%);
      color: var(--ink);
    }
    main {
      max-width: 860px;
      margin: 0 auto;
      padding: 48px 20px 72px;
    }
    .card {
      background: rgba(255, 253, 248, 0.92);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 28px;
      box-shadow: 0 18px 40px rgba(31, 41, 51, 0.08);
      backdrop-filter: blur(6px);
    }
    h1, h2 {
      margin: 0 0 12px;
      line-height: 1.15;
    }
    h1 { font-size: clamp(2rem, 5vw, 3.25rem); }
    h2 { font-size: 1.3rem; margin-top: 28px; }
    p, li { color: var(--muted); font-size: 1rem; line-height: 1.65; }
    a {
      color: var(--accent);
      font-weight: 700;
      text-decoration-thickness: 0.08em;
      text-underline-offset: 0.14em;
    }
    code {
      font-family: "SFMono-Regular", "Menlo", "Monaco", monospace;
      background: rgba(11, 110, 79, 0.08);
      border-radius: 6px;
      padding: 0.15em 0.35em;
      color: #083d2c;
    }
    ul {
      margin: 10px 0 0;
      padding-left: 1.25rem;
    }
    .spec-link {
      display: inline-block;
      margin-top: 12px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(11, 110, 79, 0.18);
      background: rgba(11, 110, 79, 0.08);
      text-decoration: none;
    }
  </style>
</head>
<body>
  <main>
    <section class="card">
      <h1>Realtime Chat AsyncAPI</h1>
      <p>
        The OpenAPI page at <code>/docs</code> does not include Socket.IO events.
        This page exposes the realtime Socket.IO contract separately for the PRO worker.
      </p>
      <p>
        <a class="spec-link" href="/asyncapi.yaml">Download the AsyncAPI YAML</a>
      </p>
      <h2>Connection</h2>
      <ul>
        <li>Connect a Socket.IO client to <code>/chat/realtime</code>.</li>
        <li>Pick the provider with <code>auth.provider</code> or <code>?provider=codex|claude</code>.</li>
        <li>If omitted, the configured server default is used and falls back to <code>CODEX</code>.</li>
      </ul>
      <h2>Client Events</h2>
      <ul>
        <li><code>user_message</code> with <code>{"content":"..."}</code> or a plain string payload.</li>
        <li><code>ping</code> with any payload.</li>
        <li><code>close</code> with any payload.</li>
      </ul>
      <h2>Server Events</h2>
      <ul>
        <li><code>ready</code>, <code>pong</code>, <code>turn_started</code>, <code>assistant_message</code>, <code>done</code>, <code>error</code></li>
        <li><code>claude</code> also emits <code>delta</code></li>
        <li><code>codex</code> also emits <code>agent_stdout</code>, <code>agent_stderr</code>, and <code>agent_event</code></li>
      </ul>
    </section>
  </main>
</body>
</html>
"""
    return HTMLResponse(content=html)
