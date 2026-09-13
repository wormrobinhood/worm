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
CLOSED_MARKERS = ("has been closed", "target closed")   # Playwright's wording when the page, context or browser is gone
ALLOW = {"www.ponsfamily.com", "ponsfamily.com"}
IDLE_EVERY = 30
WAITING = "waiting for the next graduation"     # every idle caption starts with it, so a viewer knows the state at a glance
ACTION_SPOTS = {"claim": "Payable", "forward": "Payable", "burn": "Recent trades", "compute": "Market cap", "give": "Market cap",
                "launch": "About"}               # where on the page the worm's eye goes for each kind of transaction
VIEW = {"width": 1100, "height": 690}
LAUNCHPAD = "https://www.ponsfamily.com/launchpad"
NEWEST_LAUNCHES = LAUNCHPAD + "?sort=newest"      # the Explore section, newest first; the site keeps the sort in the URL
BIGGEST_CURVES = LAUNCHPAD + "?sort=marketCap"    # the Explore section by market cap: closest to the graduation threshold
CREATE_PAGE = LAUNCHPAD + "/create"               # where a launch happens on the site: shown while the worm's own goes out


def page_gone(page, exc):
    """True when the page, its context or the browser behind it is gone: the page says it is closed, or the
    error reads like Playwright's "Target page, context or browser has been closed". A step that fails this
    way must end the session (run() then starts a fresh browser) instead of logging the same failure forever."""
    try:
        if page.is_closed():
            return True
    except Exception:
        return True
    text = str(exc).lower()
    return any(m in text for m in CLOSED_MARKERS)


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
    def __init__(self, hub, newest=None, own=None):
        super().__init__(daemon=True, name="screen")
        self.hub = hub
        self.q = queue.Queue()
        self.newest = newest or (lambda: None)   # callable: the newest graduated launch row (token, name, symbol, grad_ts)
        self.own = own or (lambda: None)         # callable: the worm's own token row (token, name, symbol, ts), once launched
        self.idle_n = 0

    def on_dig(self, ev):
        self.q.put(ev)

    def on_action(self, ev):
        """A transaction the worm just sent or settled: shown on the page it concerns, captioned exactly."""
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
                log.warning("screen session died, restarting in 10 s: %s", str(e)[:200])
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
                    if ev.get("action"):
                        self._act(page, ev)
                    else:
                        self._dig_step(page, ev)
                        last_dig = ev.get("token")
                    last_idle = time.time()             # what was just shown holds for a whole turn before the idle views resume
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

    def _frame(self, page, note, focus=None, idle=False, action=None, done=False, lag=None):
        """One frame to the page: the screenshot, a caption, where the worm's eye should go, whether the worm
        is between digs (the page then says it is waiting for the next graduation), and, for a transaction
        it sent, which action it was, whether it has settled and how many seconds the screen was behind."""
        raw = page.screenshot(type="jpeg", quality=45)
        self.hub.frame({"jpg": base64.b64encode(raw).decode(), "note": note[:160], "focus": focus,
                        "url": page.url, "ts": int(time.time()), "idle": idle, "action": action, "done": done, "lag_s": lag})

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
        if ev.get("ts"):
            log.info("screen lag %ss on dig step %s", max(0, int(time.time()) - int(ev["ts"])), step)
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
            if page_gone(page, e):
                raise
            log.info("screen step %s failed: %s", step, str(e)[:120])

    def _act(self, page, ev):
        """The worm is sending, or has just settled, a transaction: open the page it concerns (its own token's,
        or the launchpad when it has none yet) and caption exactly what is happening. The browser holds no
        key and signs nothing; it only shows."""
        token = ev.get("token")
        try:
            url = f"{LAUNCHPAD}/{token}" if token else (CREATE_PAGE if ev["action"] == "launch" else NEWEST_LAUNCHES)
            if page.url != url:
                self._goto(page, url)
            spot = ACTION_SPOTS.get(ev["action"], "Market cap")
            lag = max(0, int(time.time()) - int(ev.get("ts") or time.time()))
            note = ev["text"] + (f" · on screen {lag}s after it happened" if lag else " · on screen as it happened")
            log.info("screen lag %ss on %s", lag, ev["action"])
            self._frame(page, note, self._focus(page, spot), action=ev["action"], done=bool(ev.get("done")), lag=lag)
        except Exception as e:
            if page_gone(page, e):
                raise
            log.info("screen action %s failed: %s", ev.get("action"), str(e)[:120])

    def _idle(self, page, last_dig):
        """Views taken in turns while the worm waits for the next graduation: the newest graduation's page,
        the curve newest first, the curve biggest first and, once it has one, its own token's page. Every
        caption says so first."""
        self.idle_n += 1
        mine = None
        try:
            mine = self.own()
        except Exception as e:
            log.info("own token lookup failed: %s", str(e)[:120])
        views = ("graduation", "newest", "biggest") + (("own",) if mine and mine.get("token") else ())
        what = views[self.idle_n % len(views)]
        row = None
        try:
            row = self.newest()
        except Exception as e:
            log.info("newest graduation lookup failed: %s", str(e)[:120])
        token = (row or {}).get("token") or last_dig
        if what == "graduation" and not token:
            what = "newest"
        try:
            if what == "own":
                label = f"{mine.get('name') or 'its token'} (${mine.get('symbol') or '?'})"
                when = _ago(mine.get("ts"))
                self._goto(page, f"{LAUNCHPAD}/{mine['token']}", 2500)
                self._frame(page, f"{WAITING} · its own token: {label}" + (f", launched {when}" if when else ""),
                            self._focus(page, "Market cap"), idle=True)
            elif what == "graduation":
                label = f"{row['name']} (${row['symbol']})" if row and row.get("symbol") else token[:10]
                when = _ago(row.get("grad_ts")) if row else ""
                self._goto(page, f"{LAUNCHPAD}/{token}", 2500)
                self._frame(page, f"{WAITING} · the newest so far: {label}" + (f", graduated {when}" if when else ""),
                            self._focus(page, "Market cap"), idle=True)
            elif what == "newest":
                self._goto(page, NEWEST_LAUNCHES, 3000)
                self._scroll_to_heading(page, "Explore")
                self._frame(page, f"{WAITING} · watching the curve, newest launches first",
                            self._focus_loc(page, page.get_by_role("tab", name="Newest")), idle=True)
            else:
                self._goto(page, BIGGEST_CURVES, 3000)
                self._scroll_to_heading(page, "Explore")
                self._frame(page, f"{WAITING} · watching the curve, biggest market caps first: the next one comes from here",
                            self._focus_loc(page, page.get_by_role("tab", name="Market cap")), idle=True)
        except Exception as e:
            if page_gone(page, e):
                raise
            log.info("idle failed: %s", str(e)[:120])
