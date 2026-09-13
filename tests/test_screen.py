"""The screen's closed-page decision, without launching a browser."""
import time

import pytest

from wormhole.screen import LAUNCHPAD, WAITING, Screen, page_gone

CLOSED = "Target page, context or browser has been closed"
TOKEN = "0x" + "ab" * 20


class FakeLoc:
    first = property(lambda self: self)

    def scroll_into_view_if_needed(self, **kw):
        pass

    def click(self, **kw):
        pass

    def bounding_box(self, **kw):
        return {"x": 100, "y": 200, "width": 20, "height": 10}


class FakeMouse:
    def __init__(self):
        self.wheels = []

    def wheel(self, dx, dy):
        self.wheels.append(dy)


class FakePage:
    def __init__(self, closed=False, error=None, at_end=False):
        self.closed, self.error, self.url, self.at_end = closed, error, LAUNCHPAD, at_end
        self.mouse = FakeMouse()
        self.evaluated = []

    def is_closed(self):
        return self.closed

    def goto(self, url, **kw):
        if self.error:
            raise self.error
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, **kw):
        return b"jpeg"

    def evaluate(self, script, *a, **k):
        self.evaluated.append((script, a))
        if "scrollHeight" in script:
            return self.at_end

    def get_by_role(self, *a, **k):
        return FakeLoc()

    def get_by_text(self, *a, **k):
        if self.error:
            raise self.error
        return FakeLoc()


class Sink:
    def __init__(self):
        self.frames = []

    def frame(self, d):
        self.frames.append(d)


def test_page_gone_decision():
    assert page_gone(FakePage(closed=True), RuntimeError("anything"))
    assert page_gone(FakePage(), RuntimeError(CLOSED))
    assert page_gone(FakePage(), RuntimeError("Target closed"))
    assert not page_gone(FakePage(), RuntimeError("Timeout 2500ms exceeded."))
    assert not page_gone(FakePage(), ValueError("not allowlisted: evil.example"))

    class Dead:
        def is_closed(self):
            raise RuntimeError("Connection closed")
    assert page_gone(Dead(), RuntimeError("x"))


def test_dig_step_reraises_when_the_page_is_gone():
    s = Screen(Sink())
    ev = {"step": "start", "token": TOKEN, "data": {"name": "Tok"}}
    with pytest.raises(RuntimeError, match="has been closed"):
        s._dig_step(FakePage(error=RuntimeError(CLOSED)), ev)
    with pytest.raises(RuntimeError, match="Timeout"):
        s._dig_step(FakePage(closed=True, error=RuntimeError("Timeout 45000ms exceeded.")), ev)
    s._dig_step(FakePage(error=RuntimeError("Timeout 45000ms exceeded.")), ev)   # an ordinary failure is logged and swallowed
    s._dig_step(FakePage(error=RuntimeError("no locator")), {"step": "creator", "token": TOKEN, "text": "x"})


def test_idle_looks_through_the_graduated_launches_and_says_it_is_waiting():
    """Between digs the screen opens the launchpad's graduated launches and scrolls a step further every turn;
    every caption starts by saying the worm waits, names the newest graduation, and the frame is flagged idle."""
    sink = Sink()
    s = Screen(sink, newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": time.time() - 120})
    page = FakePage()
    for _ in range(3):
        s._idle(page, None)
    assert len(sink.frames) == 3 and page.url == LAUNCHPAD + "?sort=newest"
    assert all(f["idle"] is True and f["note"].startswith(WAITING + " · looking through the graduated launches") for f in sink.frames)
    assert all("the newest so far: Tok ($TOK), graduated 2m ago" in f["note"] for f in sink.frames)
    assert page.mouse.wheels == [260, 260] and any("Graduated" in str(e) for e in page.evaluated)   # one page load, then two scroll steps
    page.at_end = True
    s._idle(page, None)
    assert "back to the top of the graduated launches" in sink.frames[-1]["note"] and s.scroll_steps == 0
    s._dig_step(FakePage(), {"step": "creator", "token": TOKEN, "text": "creator 0x1: fresh"})
    f = sink.frames[-1]
    assert f["idle"] is False and f["note"] == "creator 0x1: fresh" and f["focus"] == [110 / 1100, 205 / 690]


def test_idle_reraises_when_the_page_is_gone():
    s = Screen(Sink(), newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": 1})
    with pytest.raises(RuntimeError, match="has been closed"):
        s._idle(FakePage(error=RuntimeError(CLOSED)), None)                         # the first turn is a page load
    s._idle(FakePage(error=RuntimeError("net::ERR_CONNECTION_RESET")), None)       # swallowed
    with pytest.raises(RuntimeError, match="Timeout"):
        s._idle(FakePage(closed=True, error=RuntimeError("Timeout")), None)


def test_allowlist_still_fences_the_browser():
    s = Screen(Sink())
    with pytest.raises(ValueError, match="not allowlisted"):
        s._goto(FakePage(), "https://evil.example/launchpad")
    s._goto(FakePage(), LAUNCHPAD + "/" + TOKEN)


def test_own_token_gets_a_look_every_so_often_once_it_exists():
    """Without a token of its own the screen only looks through the graduated launches; with one, every OWN_EVERY
    turns it opens its own token's page, captioned as waiting like the rest, then goes back to the top."""
    from wormhole import screen as SC
    sink = Sink()
    s = Screen(sink, newest=lambda: None, own=lambda: None)
    page = FakePage()
    for _ in range(SC.OWN_EVERY + 1):
        s._idle(page, None)
    assert {f["url"] for f in sink.frames} == {LAUNCHPAD + "?sort=newest"}
    mine = {"token": "0x" + "cd" * 20, "name": "Worm", "symbol": "WORM", "ts": time.time() - 300}
    sink2 = Sink()
    s2 = Screen(sink2, newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": time.time() - 60}, own=lambda: mine)
    page2 = FakePage()
    for _ in range(SC.OWN_EVERY + 1):
        s2._idle(page2, None)
    own = [f for f in sink2.frames if f["url"] == LAUNCHPAD + "/" + mine["token"]]
    assert len(own) == 1 and own[0]["idle"] is True
    assert own[0]["note"] == WAITING + " · its own token: Worm ($WORM), launched 5m ago"
    assert sink2.frames[-1]["url"] == LAUNCHPAD + "?sort=newest" and "looking through" in sink2.frames[-1]["note"]


def test_a_launch_in_flight_shows_the_create_page_and_the_screen_lag():
    from wormhole.screen import CREATE_PAGE
    sink = Sink()
    s = Screen(sink)
    page = FakePage()
    s._act(page, {"action": "launch", "text": "launching its own token Worm ($WORM): sent as 0x12…, waiting for the block", "token": None, "done": False, "ts": int(time.time()) - 3})
    f = sink.frames[-1]
    assert f["url"] == CREATE_PAGE and f["action"] == "launch" and f["done"] is False
    assert f["lag_s"] == 3 and f["note"].endswith(" · on screen 3s after it happened")
    tok = "0x" + "cd" * 20
    n = len(sink.frames)
    s._act(page, {"action": "launch", "text": "launched its own token Worm ($WORM) just now", "token": tok, "done": True, "ts": int(time.time())})
    first, last = sink.frames[n], sink.frames[-1]
    assert first["url"] == CREATE_PAGE and first["action"] == "launch" and first["done"] is True     # said at once, on the page that was up
    assert last["url"] == LAUNCHPAD + "/" + tok and last["done"] is True and last["note"].endswith(" · on screen as it happened")
