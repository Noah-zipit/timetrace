"""Capture timetrace UI screenshots and the demo video.

The bundled Chromium (152) hard-blocks direct localhost navigation
(ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS), so all traffic goes through a
request-interception shim that replays each request with plain HTTP from the VM
and fulfills it in-page. The browser renders the REAL UI with REAL API data.

Usage:
    python3 scripts/demo_capture.py          # screenshots only
    python3 scripts/demo_capture.py --video   # screenshots + webm walkthrough
"""
import http.client
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8123"
CHROME = "/opt/meta-chromium/chrome"
OUT = Path(__file__).resolve().parent.parent / "ui" / "qa"
QUESTION = 'Is it safe to change "rich/console.py" today?'


def fetch_raw(method, url, headers, body):
    u = urllib.parse.urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    h = {k: v for k, v in headers.items()
         if k.lower() not in ("host", "content-length")}
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    status = r.status
    hdrs = dict(r.getheaders())
    conn.close()
    return status, hdrs, data


def shim(page):
    def handler(route):
        req = route.request
        try:
            status, headers, body = fetch_raw(
                req.method, req.url, req.headers, req.post_data_buffer)
            route.fulfill(status=status, headers=headers, body=body)
        except Exception:
            route.abort(error_code="failed")
    page.route("**/*", handler)


def new_page(p, record_video=False):
    b = p.chromium.launch(executable_path=CHROME,
                          args=["--no-sandbox", "--disable-gpu"])
    if record_video:
        ctx = b.new_context(viewport={"width": 1280, "height": 800},
                            record_video_dir=str(OUT / "raw"),
                            record_video_size={"width": 1280, "height": 800})
        pg = ctx.new_page()
    else:
        pg = b.new_page(viewport={"width": 1280, "height": 800})
    shim(pg)
    return b, pg


def ask(pg, question):
    pg.fill("#ask-input", question)
    pg.click("#ask-button")
    pg.wait_for_selector("#verdict-card:not([hidden])", timeout=90000)
    pg.wait_for_timeout(800)


def scrub_to(pg, index):
    pg.eval_on_selector("#scrubber",
                        f"el => {{ el.value = '{index}'; "
                        "el.dispatchEvent(new Event('input', {bubbles: true})); }}")
    pg.wait_for_timeout(6000)


def screenshots():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b, pg = new_page(p)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(BASE + "/")
        pg.wait_for_timeout(3000)
        pg.screenshot(path=str(OUT / "01-fresh-load.png"))
        print("fresh load — modules:",
              pg.eval_on_selector_all("#module-tree .module-row",
                                      "els => els.length"))

        ask(pg, QUESTION)
        pg.screenshot(path=str(OUT / "02-verdict-now.png"))
        v_now = pg.text_content("#verdict-badge").strip()
        print("verdict now:", v_now, "| proof steps:",
              pg.eval_on_selector_all("#proof-chain li", "els => els.length"))

        scrub_to(pg, 2)
        pg.screenshot(path=str(OUT / "03-verdict-scrubbed.png"))
        v_old = pg.text_content("#verdict-badge").strip()
        print("verdict scrubbed:", v_old, "|",
              pg.text_content("#state-label").strip())
        print("page errors:", errors if errors else "none")
        b.close()
        return v_now, v_old


def video():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b, pg = new_page(p, record_video=True)
        pg.goto(BASE + "/")
        pg.wait_for_timeout(3500)                       # settle on timeline
        ask(pg, QUESTION)                              # the money question
        pg.wait_for_timeout(2500)                      # hold the verdict
        scrub_to(pg, 2)                                # travel back in time
        pg.wait_for_timeout(2500)                      # hold changed verdict
        scrub_to(pg, 60)                               # back to the present
        pg.wait_for_timeout(2000)
        path = pg.video.path()
        b.close()
        final = OUT / "timetrace-demo-raw.webm"
        Path(path).rename(final)
        print("video:", final)
        return final


if __name__ == "__main__":
    if "--video" in sys.argv:
        video()
    else:
        screenshots()
