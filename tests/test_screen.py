"""The screen's closed-page decision, without launching a browser."""
import pytest

from wormhole.screen import LAUNCHPAD, Screen, page_gone

CLOSED = "Target page, context or browser has been closed"
TOKEN = "0x" + "ab" * 20


class FakePage:
    def __init__(self, closed=False, error=None):
        self.closed, self.error, self.url = closed, error, LAUNCHPAD

    def is_closed(self):
        return self.closed

    def goto(self, url, **kw):
        if self.error:
            raise self.error

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, **kw):
        return b"jpeg"

    def get_by_text(self, *a, **k):
        raise self.error or RuntimeError("no locator")


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
