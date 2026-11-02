"""The Runner site's one page frame: a generated report links /static/site/site.css and carries the site header, as
the runner shell (runner.html) does."""

STYLESHEET = "/static/site/site.css"
LINK = f'<link rel="stylesheet" href="{STYLESHEET}">'
NAV = (("/", "Runs"), ("/coverage/", "Coverage"))


def header(here: str = "") -> str:
    links = "".join(
        f'<a class="btn" href="{path}"{" aria-current=page" if path == here else ""}>{name}</a>' for path, name in NAV
    )
    return f'<header><a class="logo" href="/">decomp</a><nav class="nav" aria-label="site">{links}</nav></header>'


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
