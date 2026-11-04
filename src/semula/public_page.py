"""The one page every screen semula serves itself shares (SEM-131): sign-in, sign-up, device
approval, invitations, the terms, the rate-limit page and the browser's 404.

One inline stylesheet built from the dashboard's tokens (``dashboard/theme.py``, which a test
keeps equal to ``docs/design/tokens.json``): light and dark by ``prefers-color-scheme``, the
mark and wordmark, a centred card. It loads nothing (system fonts, no script, no request), so
the page's policy stays ``default-src 'none'`` with the stylesheet allowed by its hash. Every
dynamic value must be escaped by the caller; ``title`` is escaped here.
"""

from __future__ import annotations

import base64
import hashlib
import html

from fastapi.responses import HTMLResponse

from .dashboard.theme import DARK, LIGHT

DESCRIPTION = "A mock LLM server your coding agent answers."


def _vars(tokens: dict[str, str]) -> str:
    return "".join(f"--{name}:{value};" for name, value in tokens.items())


# Written with attributes only: a ``style=`` attribute would need 'unsafe-inline'.
MARK = (
    '<svg class=mark viewBox="0 0 64 64" width=32 height=32 aria-hidden=true focusable=false>'
    '<path d="M32 11A21 21 0 1 1 11 32" fill="none" stroke="currentColor" stroke-width="6.5" '
    'stroke-linecap="round"/>'
    '<path d="M11 22L4 33H18z" fill="currentColor" stroke="currentColor" stroke-width="2" '
    'stroke-linejoin="round"/>'
    '<circle cx="32" cy="11" r="8.5" fill="#C8271F"/></svg>'
)

MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
STYLE = (
    f":root{{color-scheme:light dark;{_vars(LIGHT)}}}"
    f"@media (prefers-color-scheme:dark){{:root{{{_vars(DARK)}}}}}"
    "*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:flex;flex-direction:column;background:var(--bg);"
    "color:var(--text);font:16px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    "header,footer{width:100%;max-width:40rem;margin:0 auto;padding:1.25rem 1rem}"
    "header{display:flex;align-items:center;gap:.25rem 1rem;flex-wrap:wrap}"
    ".brand{display:flex;align-items:center;gap:.6rem;color:var(--text);text-decoration:none;"
    "min-height:44px}"
    ".brand b{font-size:1.25rem;letter-spacing:.01em}"
    ".mark{color:var(--text);flex:none}"
    ".tag{margin:0;color:var(--muted);font-size:.875rem;flex:1 1 14rem}"
    "main{flex:1;width:100%;padding:0 1rem}"
    ".card{max-width:26rem;margin:1rem auto 2rem;padding:1.75rem;background:var(--surface);"
    "border:1px solid var(--outline-soft);border-radius:16px}"
    ".card.wide{max-width:65ch;line-height:1.6}"
    "h1{margin:0 0 1rem;font-size:1.5rem;line-height:1.25}"
    "h2{margin:1.5rem 0 .5rem;font-size:1.125rem;line-height:1.3}"
    "p{margin:.75rem 0}ul{padding-left:1.25rem}li{margin:.4rem 0}"
    "a{color:var(--primary);text-underline-offset:.15em}"
    f"code{{font:.9em {MONO};background:var(--surface-2);padding:.1em .35em;border-radius:4px}}"
    f".code{{font:600 1.75rem/1.2 {MONO};"
    "letter-spacing:.12em;text-align:center;margin:1rem 0;padding:.75rem;border-radius:12px;"
    "background:var(--primary-container);color:var(--on-primary-container);"
    "overflow-wrap:anywhere}"
    "small{color:var(--muted)}"
    ".btn,button,input{font:inherit}"
    ".btn,button{display:flex;align-items:center;justify-content:center;width:100%;"
    "min-height:44px;margin:.6rem 0;padding:.5rem 1.25rem;border-radius:22px;cursor:pointer;"
    "text-decoration:none;text-align:center;border:1px solid var(--primary);"
    "background:var(--primary);color:var(--on-primary)}"
    ".btn.alt,button.alt{background:transparent;color:var(--primary);border-color:var(--outline)}"
    ".btn:hover,button:hover{filter:brightness(1.08)}"
    ".btn.alt:hover,button.alt:hover{background:var(--surface-2);filter:none}"
    "input{display:block;width:100%;min-height:44px;padding:.5rem .75rem;"
    "border:1px solid var(--outline);border-radius:8px;background:var(--surface);"
    "color:var(--text)}"
    "a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--primary);"
    "outline-offset:2px}"
    ".actions{display:flex;gap:.75rem;flex-wrap:wrap}.actions button{flex:1 1 8rem}"
    "footer{color:var(--muted);font-size:.875rem;display:flex;gap:.5rem 1.25rem;"
    "flex-wrap:wrap;justify-content:center;text-align:center}"
    "footer a{color:var(--muted);display:inline-flex;align-items:center;min-height:44px}"
)
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
# These pages run no script and cannot be framed; their one style block is allowed by its hash,
# and the only thing they submit goes back to this site.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"style-src 'sha256-{STYLE_HASH}'; img-src data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def render(title: str, body: str, *, wide: bool = False) -> str:
    safe = html.escape(title)
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=color-scheme content='light dark'>"
        f"<title>{safe} - semula</title><link rel=icon href='data:,'><style>{STYLE}</style>"
        f'<header><a class=brand href="/">{MARK}<b>semula</b></a>'
        f"<p class=tag>{DESCRIPTION}</p></header>"
        f"<main><div class='card{' wide' if wide else ''}'><h1>{safe}</h1>{body}</div></main>"
        '<footer><a href="/terms">Terms</a><a href="/auth/login">Sign in</a></footer>'
    )


def page(title: str, body: str, status: int = 200, *, wide: bool = False) -> HTMLResponse:
    return HTMLResponse(render(title, body, wide=wide), status_code=status, headers=HEADERS)
