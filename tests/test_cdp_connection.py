"""Private browser discovery uses the configured host and rejects redirects/embedded credentials."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from wormhole import screen


def test_private_discovery_rewrites_loopback(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {'webSocketDebuggerUrl': 'ws://localhost:9222/devtools/browser/fixture'}
    session = Mock()
    session.get.return_value = response
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(screen.requests, 'Session', lambda: session)
    chromium = Mock()
    screen.connect_remote_browser(SimpleNamespace(chromium=chromium), 'http://browser.railway.internal:9223')
    assert session.trust_env is False
    session.get.assert_called_once_with('http://browser.railway.internal:9223/json/version',
                                       headers={'Host': 'localhost'}, timeout=10, allow_redirects=False)
    chromium.connect_over_cdp.assert_called_once_with('ws://browser.railway.internal:9223/devtools/browser/fixture',
                                                      headers={'Host': 'localhost'})


def test_cdp_rejects_credentials_before_network():
    with pytest.raises(ValueError):
        screen.connect_remote_browser(None, 'http://user:example@browser.railway.internal:9223')
