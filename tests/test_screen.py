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


class FakePage:
    def __init__(self, closed=False, error=None):
        self.closed, self.error, self.url = closed, error, LAUNCHPAD

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

    def evaluate(self, *a, **k):
        pass

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


def test_idle_frames_say_the_worm_is_waiting():
    """Between digs every caption starts by saying the worm waits for the next graduation, whichever of the
    three views it is on, and the frame is flagged idle; a dig frame is not."""
    sink = Sink()
    s = Screen(sink, newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": time.time() - 120})
    for _ in range(3):
        s._idle(FakePage(), None)
    assert len(sink.frames) == 3
    assert all(f["idle"] is True and f["note"].startswith(WAITING + " · ") for f in sink.frames)
    assert all("idle:" not in f["note"] for f in sink.frames)
    notes = "\n".join(f["note"] for f in sink.frames)
    assert "the newest so far: Tok ($TOK), graduated 2m ago" in notes
    assert "newest launches first" in notes and "biggest market caps first" in notes
    assert {f["url"] for f in sink.frames} == {LAUNCHPAD + "/" + TOKEN, LAUNCHPAD + "?sort=newest", LAUNCHPAD + "?sort=marketCap"}
    s._dig_step(FakePage(), {"step": "creator", "token": TOKEN, "text": "creator 0x1: fresh"})
    f = sink.frames[-1]
    assert f["idle"] is False and f["note"] == "creator 0x1: fresh" and f["focus"] == [110 / 1100, 205 / 690]


def test_idle_reraises_when_the_page_is_gone():
    s = Screen(Sink(), newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": 1})
    s.idle_n = 2                                              # the next turn is the graduation view: a page load
    with pytest.raises(RuntimeError, match="has been closed"):
        s._idle(FakePage(error=RuntimeError(CLOSED)), None)
    s.idle_n = 2
    s._idle(FakePage(error=RuntimeError("net::ERR_CONNECTION_RESET")), None)       # swallowed
    s.idle_n = 2
    with pytest.raises(RuntimeError, match="Timeout"):
        s._idle(FakePage(closed=True, error=RuntimeError("Timeout")), None)


def test_allowlist_still_fences_the_browser():
    s = Screen(Sink())
    with pytest.raises(ValueError, match="not allowlisted"):
        s._goto(FakePage(), "https://evil.example/launchpad")
    s._goto(FakePage(), LAUNCHPAD + "/" + TOKEN)


def test_own_token_joins_the_idle_rotation_once_it_exists():
    """Without a token of its own the screen cycles three views; with one, a fourth: its own token's page,
    captioned as waiting like the rest."""
    sink = Sink()
    s = Screen(sink, newest=lambda: None, own=lambda: None)
    for _ in range(4):
        s._idle(FakePage(), None)
    newest, biggest = LAUNCHPAD + "?sort=newest", LAUNCHPAD + "?sort=marketCap"
    assert [f["url"] for f in sink.frames] == [newest, biggest, newest, newest]   # three views; no graduation yet, so newest stands in for it
    mine = {"token": "0x" + "cd" * 20, "name": "Worm", "symbol": "WORM", "ts": time.time() - 300}
    sink2 = Sink()
    s2 = Screen(sink2, newest=lambda: {"token": TOKEN, "name": "Tok", "symbol": "TOK", "grad_ts": time.time() - 60}, own=lambda: mine)
    for _ in range(4):
        s2._idle(FakePage(), None)
    own = [f for f in sink2.frames if f["url"] == LAUNCHPAD + "/" + mine["token"]]
    assert len(own) == 1 and own[0]["idle"] is True
    assert own[0]["note"] == WAITING + " · its own token: Worm ($WORM), launched 5m ago"
    assert len({f["url"] for f in sink2.frames}) == 4
