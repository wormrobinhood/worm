"""The worm's screen: a headless Chromium that opens what the worm is reading, streamed as JPEG frames.

Fenced on purpose: an allowlist of hosts, no wallet or keys in this browser, no downloads, no dialogs,
and it never follows a link on its own. During a dig it goes where the scorer's events point. Idle, it
takes turns between the newest graduation's page, the launchpad's newest launches and the curves with the
biggest market caps (the next graduations come from there). The launchpad's own Graduated grid sorts by
market cap with no sort control, so the same five big tokens would sit at the top forever."""
import base64
import logging
import queue
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("wormhole.screen")
ALLOW = {"www.ponsfamily.com", "ponsfamily.com"}
IDLE_EVERY = 30
VIEW = {"width": 1100, "height": 690}
LAUNCHPAD = "https://www.ponsfamily.com/launchpad"
NEWEST_LAUNCHES = LAUNCHPAD + "?sort=newest"      # the Explore section, newest first; the site keeps the sort in the URL
BIGGEST_CURVES = LAUNCHPAD + "?sort=marketCap"    # the Explore section by market cap: closest to the graduation threshold


def _ago(ts):
    d = max(0, int(time.time()) - int(ts or 0))
    if not ts:
        return ""
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    return f"{d // 3600}h {d % 3600 // 60}m ago"


class Screen(threading.Thread):
    def __init__(self, hub, newest=None):
        super().__init__(daemon=True, name="screen")
        self.hub = hub
        self.q = queue.Queue()
        self.newest = newest or (lambda: None)   # callable: the newest graduated launch row (token, name, symbol, grad_ts)
        self.idle_n = 0

    def on_dig(self, ev):
        self.q.put(ev)

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("playwright is not installed: screen off")
            return
        while True:
            try:
                self._session(sync_playwright)
            except Exception as e:
                log.warning("screen session died: %s", str(e)[:200])
                time.sleep(10)

    def _session(self, sync_playwright):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport=VIEW, color_scheme="dark", accept_downloads=False)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.dismiss())
            last_idle, last_dig = 0.0, None
            log.info("screen on")
            while True:
                try:
                    ev = self.q.get(timeout=5)
                except queue.Empty:
                    ev = None
                if ev:
                    self._dig_step(page, ev)
                    last_dig = ev.get("token")
                    continue
                if time.time() - last_idle > IDLE_EVERY:
                    last_idle = time.time()
                    self._idle(page, last_dig)

    def _goto(self, page, url, wait=2500):
        host = urlparse(url).hostname or ""
        if host not in ALLOW:
            raise ValueError(f"not allowlisted: {host}")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(wait)

    def _frame(self, page, note, focus=None):
        raw = page.screenshot(type="jpeg", quality=45)
        self.hub.frame({"jpg": base64.b64encode(raw).decode(), "note": note[:160], "focus": focus,
                        "url": page.url, "ts": int(time.time())})

    def _focus(self, page, text, click=False):
        return self._focus_loc(page, page.get_by_text(text, exact=False).first, click)

    def _focus_loc(self, page, loc, click=False):
        """Scroll an element into view (optionally click it) and return its centre as a fraction of the viewport."""
        try:
            loc.scroll_into_view_if_needed(timeout=2500)
            if click:
                loc.click(timeout=1500)
                page.wait_for_timeout(1200)
            page.wait_for_timeout(400)
            box = loc.bounding_box(timeout=1500)
            if box:
                return [(box["x"] + box["width"] / 2) / VIEW["width"], (box["y"] + box["height"] / 2) / VIEW["height"]]
        except Exception:
            pass
        return None

    def _scroll_to_heading(self, page, text, offset=72):
        """Put the section whose heading starts with `text` at the top, just under the launchpad's sticky nav."""
        page.evaluate(
            "([text, offset]) => { const h = [...document.querySelectorAll('h1,h2')]"
            ".find(e => e.textContent.trim().startsWith(text));"
            " if (h) window.scrollTo(0, Math.max(0, h.getBoundingClientRect().top + window.scrollY - offset)); }",
            [text, offset])
        page.wait_for_timeout(700)

    def _dig_step(self, page, ev):
        step, token, d = ev["step"], ev["token"], ev.get("data", {})
        try:
            if step == "start":
                self._goto(page, f"{LAUNCHPAD}/{token}")
                self._frame(page, f"opening the pons page of {d.get('name') or token[:10]}", self._focus(page, "About"))
            elif step == "creator":
                self._frame(page, ev["text"], self._focus(page, "Creator"))
            elif step == "curve":
                self._frame(page, ev["text"], self._focus(page, "Recent trades"))
            elif step == "holders":
                self._frame(page, ev["text"], self._focus(page, "Holders", click=True))
            elif step == "pool":
                self._frame(page, ev["text"], self._focus(page, "Market cap"))
            elif step == "verdict":
                self._frame(page, ev["text"])
        except Exception as e:
            log.info("screen step %s failed: %s", step, str(e)[:120])

    def _idle(self, page, last_dig):
        """Three views, taken in turns: the newest graduation's page, the curve newest first, the curve biggest first."""
        self.idle_n += 1
        what = ("graduation", "newest", "biggest")[self.idle_n % 3]
        row = None
        try:
            row = self.newest()
        except Exception as e:
            log.info("newest graduation lookup failed: %s", str(e)[:120])
        token = (row or {}).get("token") or last_dig
        if what == "graduation" and not token:
            what = "newest"
        try:
            if what == "graduation":
                label = f"{row['name']} (${row['symbol']})" if row and row.get("symbol") else token[:10]
                when = _ago(row.get("grad_ts")) if row else ""
                self._goto(page, f"{LAUNCHPAD}/{token}", 2500)
                self._frame(page, f"idle: newest graduation · {label}" + (f" · graduated {when}" if when else ""),
                            self._focus(page, "Market cap"))
            elif what == "newest":
                self._goto(page, NEWEST_LAUNCHES, 3000)
                self._scroll_to_heading(page, "Explore")
                self._frame(page, "idle: watching the curve, newest launches first, for the next graduation",
                            self._focus_loc(page, page.get_by_role("tab", name="Newest")))
            else:
                self._goto(page, BIGGEST_CURVES, 3000)
                self._scroll_to_heading(page, "Explore")
                self._frame(page, "idle: watching the curve, biggest market caps first: the next graduation comes from here",
                            self._focus_loc(page, page.get_by_role("tab", name="Market cap")))
        except Exception as e:
            log.info("idle failed: %s", str(e)[:120])
