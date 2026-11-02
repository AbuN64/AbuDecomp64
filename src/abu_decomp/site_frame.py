"""The Runner site's one page frame: every page links /static/site/site.css and shares the head, header and the map
sidebar (static/site/map.js, booted on its own since these pages have no runner shell)."""

from html import escape

STYLESHEET = "/static/site/site.css"
LINK = f'<link rel="stylesheet" href="{STYLESHEET}">'
NAV = (("/", "Runs"), ("/live", "Live"), ("/coverage/", "Coverage"))
SIDEBAR = (
    '<aside id="sitemap" aria-label="map"></aside>'
    '<script type="module">import { boot } from "/static/site/map.js";'
    ' boot(document.getElementById("sitemap"));</script>'
)
META = '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'


def head(title: str) -> str:
    return f"{META}<title>{escape(title)}</title>{LINK}"


def header(here: str = "") -> str:
    links = "".join(
        f'<a class="btn" href="{path}"{" aria-current=page" if path == here else ""}>{name}</a>' for path, name in NAV
    )
    return f'<header><a class="logo" href="/">decomp</a><nav class="nav" aria-label="site">{links}</nav></header>'


def document(title: str, body: str, *, here: str = "", cls: str = "page", chrome: bool = True) -> str:
    """A complete page: head with the shared stylesheet; with `chrome`, the site header and the map sidebar left of
    `body` in main; without it, `body` in main alone."""
    content = (
        f'{header(here)}<div class="frame">{SIDEBAR}<main>{body}</main></div>' if chrome else f"<main>{body}</main>"
    )
    return f'<!doctype html><html lang="en"><head>{head(title)}</head><body class="{cls}">{content}</body></html>'


def wrap_report(page: bytes) -> bytes:
    """A generated HTML report (gcovr) with the shared stylesheet linked and the site header on top."""
    text = page.decode("utf-8", "replace")
    if STYLESHEET in text:
        return page
    at = text.lower().find("</head>")
    text = text[:at] + LINK + text[at:] if at >= 0 else LINK + text
    body = text.lower().find("<body")
    if body >= 0 and (end := text.find(">", body)) >= 0:
        text = text[: end + 1] + header("/coverage/") + text[end + 1 :]
    else:
        text += header("/coverage/")
    return text.encode()
