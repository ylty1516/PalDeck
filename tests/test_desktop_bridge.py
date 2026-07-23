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
    drag = lambda window: window.calls.append('begin_drag') or True
    bridge = DesktopBridge(custom_chrome=True, native_drag=drag)
    window = FakeWindow()
    bridge.bind(window)
    assert bridge.get_state() == {"state": "normal", "custom_chrome": True}
    assert bridge.minimize() == {"state": "minimized"}
    assert bridge.toggle_maximize() == {"state": "maximized"}
    assert bridge.toggle_maximize() == {"state": "normal"}
    assert bridge.begin_drag() == {'started': True}
    assert bridge.close() == {"state": "closed"}
    assert window.calls == ['minimize', 'maximize', 'restore', 'begin_drag', 'destroy']
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


def test_folder_picker_issues_grants_without_accepting_a_frontend_path(tmp_path):
    folder = tmp_path / "mods"; folder.mkdir()
    archive = folder / "A.zip"; archive.write_bytes(b"zip")

    class PickerWindow(FakeWindow):
        def create_file_dialog(self, dialog_type, allow_multiple=False):
            self.calls.append(f"dialog:{dialog_type}:{allow_multiple}")
            return (str(folder),)

    class Registry:
        def issue(self, paths):
            assert list(paths) == [archive.resolve()]
            return [{"selection_token": "grant", "name": "A.zip", "size": 3, "kind": "zip"}]

    bridge = DesktopBridge(selection_registry=Registry())
    window = PickerWindow()
    bridge.bind(window, folder_dialog_type="folder")
    assert bridge.choose_mod_folder() == {"items": [{"selection_token": "grant", "name": "A.zip", "size": 3, "kind": "zip"}]}
    assert window.calls == ["dialog:folder:False"]
    with pytest.raises(TypeError):
        bridge.choose_mod_folder(str(folder))


def test_failed_native_call_does_not_publish_false_state():
    class BrokenWindow(FakeWindow):
        def maximize(self):
            raise RuntimeError("native failure")

    bridge = DesktopBridge()
    bridge.bind(BrokenWindow())
    with pytest.raises(RuntimeError, match="native failure"):
        bridge.toggle_maximize()
    assert bridge.get_state()["state"] == "normal"


def test_native_drag_requires_binding_and_reports_unavailable_fallback():
    bridge = DesktopBridge(native_drag=lambda _window: False)
    with pytest.raises(RuntimeError, match='not ready'):
        bridge.begin_drag()
    bridge.bind(FakeWindow())
    assert bridge.begin_drag() == {'started': False}


def test_native_drag_synchronizes_snap_state_before_next_toggle():
    class NativeWindow(FakeWindow):
        native = type('Native', (), {'WindowState': 'Maximized'})()

    bridge = DesktopBridge(native_drag=lambda _window: True)
    window = NativeWindow()
    bridge.bind(window)
    assert bridge.begin_drag() == {'started': True}
    assert bridge.get_state()['state'] == 'maximized'
    bridge.toggle_maximize()
    assert window.calls == ['restore']
