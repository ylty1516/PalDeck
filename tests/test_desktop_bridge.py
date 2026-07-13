from __future__ import annotations

import pytest

from backend.desktop_bridge import DesktopBridge


class FakeWindow:
    def __init__(self):
        self.calls: list[str] = []

    def minimize(self): self.calls.append("minimize")
    def maximize(self): self.calls.append("maximize")
    def restore(self): self.calls.append("restore")
    def destroy(self): self.calls.append("destroy")


def test_bridge_exposes_only_window_control_whitelist():
    bridge = DesktopBridge(custom_chrome=True)
    window = FakeWindow()
    bridge.bind(window)
    assert bridge.get_state() == {"state": "normal", "custom_chrome": True}
    assert bridge.minimize() == {"state": "minimized"}
    assert bridge.toggle_maximize() == {"state": "maximized"}
    assert bridge.toggle_maximize() == {"state": "normal"}
    assert bridge.close() == {"state": "closed"}
    assert window.calls == ["minimize", "maximize", "restore", "destroy"]
    for forbidden in ("execute", "open_path", "open_url", "eval", "run"):
        assert not hasattr(bridge, forbidden)


def test_bridge_requires_binding_and_rejects_actions_after_close():
    bridge = DesktopBridge(custom_chrome=False)
    assert bridge.get_state() == {"state": "normal", "custom_chrome": False}
    with pytest.raises(RuntimeError, match="not ready"):
        bridge.minimize()
    bridge.bind(FakeWindow())
    bridge.close()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.minimize()


def test_failed_native_call_does_not_publish_false_state():
    class BrokenWindow(FakeWindow):
        def maximize(self):
            raise RuntimeError("native failure")

    bridge = DesktopBridge()
    bridge.bind(BrokenWindow())
    with pytest.raises(RuntimeError, match="native failure"):
        bridge.toggle_maximize()
    assert bridge.get_state()["state"] == "normal"
