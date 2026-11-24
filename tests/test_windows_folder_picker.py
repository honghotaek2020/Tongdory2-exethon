"""Windows 기본 폴더 선택 창의 네이티브 호출과 경로 처리."""

import ctypes as ct
from types import SimpleNamespace

from tongdory2.web import windows_folder
from tongdory2.web.routes import setup as setup_routes


def _prepare(monkeypatch, result, calls):
    monkeypatch.setattr(setup_routes.sys, "platform", "win32")
    monkeypatch.setattr(setup_routes, "_running_tests", lambda: False)
    monkeypatch.setattr(setup_routes, "_is_dir", lambda path: True)

    def choose(prompt, initial, multiple):
        calls.append((prompt, initial, multiple))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(setup_routes, "choose_folders", choose)


def test_selected_korean_paths_are_returned_without_process_output(monkeypatch):
    calls = []
    paths = [r"C:\Users\홍길동\바탕 화면", r"D:\다른 폴더"]
    _prepare(monkeypatch, paths, calls)
    prompt, initial = "정리 대상 폴더 선택", r"C:\Users\홍길동"
    assert setup_routes._pick_folders_windows(prompt, initial, True) == {"paths": paths}
    assert calls == [(prompt, initial, True)]


def test_cancel_and_windows_api_failure(monkeypatch, caplog):
    for result, expected in (
        (None, {"cancelled": True, "paths": []}),
        ([], {"fallback": True, "paths": []}),
        (OSError("Show (HRESULT 0x80070005)"), {"fallback": True, "paths": []}),
    ):
        _prepare(monkeypatch, result, [])
        assert setup_routes._pick_folders_windows("p", "", False) == expected
    assert "0x80070005" in caplog.text

    _prepare(monkeypatch, [r"C:\gone"], [])
    monkeypatch.setattr(setup_routes, "_is_dir", lambda path: False)
    assert setup_routes._pick_folders_windows("p", "", False) == {"fallback": True, "paths": []}


def test_non_windows_and_tests_never_open_the_dialog(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_routes, "choose_folders", lambda *args: calls.append(args))
    monkeypatch.setattr(setup_routes.sys, "platform", "darwin")
    assert setup_routes._pick_folders_windows("p", "", False) == {"fallback": True, "paths": []}
    monkeypatch.setattr(setup_routes.sys, "platform", "win32")
    assert setup_routes._pick_folders_windows("p", "", False) == {"fallback": True, "paths": []}
    assert calls == []


def test_windows_dialog_selects_existing_folders_and_releases_com_resources(monkeypatch):
    events = []
    paths = [r"C:\한글 폴더", r"D:\공백 포함"]
    buffers = [ct.create_unicode_buffer(path) for path in paths]

    def assign(pointer, ctype, value):
        ct.cast(pointer, ct.POINTER(ctype))[0] = value

    def create_dialog(_cls, _outer, _context, _iid, output):
        assign(output, ct.c_void_p, 1)
        return 0

    def create_folder(path, _bind_context, _iid, output):
        events.append(("initial", path))
        assign(output, ct.c_void_p, 2)
        return 0

    ole = SimpleNamespace(
        CoInitializeEx=lambda _, mode: 0 if mode == 2 else -1,
        CoCreateInstance=create_dialog,
        CoTaskMemFree=lambda address: events.append(("free", address.value)),
        CoUninitialize=lambda: events.append(("uninitialize",)),
    )
    shell = SimpleNamespace(SHCreateItemFromParsingName=create_folder)
    monkeypatch.setattr(ct, "WinDLL", lambda name: {"ole32.dll": ole, "shell32.dll": shell}[name], raising=False)

    def method(interface, slot, *_types, **_options):
        def call(_this, *args):
            if slot == 2:  # IUnknown::Release
                events.append(("release", interface.value))
            elif interface.value == 1 and slot == 10:  # IFileDialog::GetOptions
                assign(args[0], windows_folder.win.DWORD, 8)
            elif interface.value == 1 and slot == 9:
                events.append(("flags", args[0]))
            elif interface.value == 1 and slot == 17:
                events.append(("title", args[0]))
            elif interface.value == 1 and slot == 11:
                assert args[0].value == 2
            elif interface.value == 1 and slot == 27:  # IFileOpenDialog::GetResults
                assign(args[0], ct.c_void_p, 3)
            elif interface.value == 3 and slot == 7:
                assign(args[0], windows_folder.win.DWORD, len(paths))
            elif interface.value == 3 and slot == 8:
                assign(args[1], ct.c_void_p, 4 + args[0])
            elif interface.value in (4, 5) and slot == 5:  # IShellItem::GetDisplayName
                assert args[0] == 0x80058000
                assign(args[1], ct.c_void_p, ct.addressof(buffers[interface.value - 4]))
            else:
                raise AssertionError((interface.value, slot))
            return 0
        return call

    monkeypatch.setattr(windows_folder, "_method", method)
    monkeypatch.setattr(windows_folder, "_show", lambda dialog: 0)
    assert windows_folder.choose_folders("정리 대상 폴더 선택", paths[0], True) == paths
    assert ("flags", 0xA68) in events
    assert ("title", "정리 대상 폴더 선택") in events and ("initial", paths[0]) in events
    assert [event for event in events if event[0] == "release"] == [
        ("release", 2), ("release", 4), ("release", 5), ("release", 3), ("release", 1),
    ]
    assert [event[1] for event in events if event[0] == "free"] == [ct.addressof(buffer) for buffer in buffers]
    assert events[-1] == ("uninitialize",)

    events.clear()
    monkeypatch.setattr(windows_folder, "_show", lambda dialog: windows_folder._CANCELLED)
    assert windows_folder.choose_folders("폴더 선택", "", False) is None
    assert events == [("flags", 0x868), ("title", "폴더 선택"), ("release", 1), ("uninitialize",)]


def test_dialog_uses_foreground_browser_and_closes_on_timeout(monkeypatch):
    callbacks, closed, stopped = [], [], []
    now = 0.0
    dialog = ct.c_void_p(1)

    def class_name(_owner, buffer, _length):
        buffer.value = "Chrome_WidgetWin_1"
        return len(buffer.value)

    def set_timer(_owner, _timer, milliseconds, callback):
        assert milliseconds == 300_000
        callbacks.append(callback)
        return 7

    def show(_dialog, owner):
        nonlocal now
        assert owner == 123
        callbacks[0](None, 0, 7, 0)
        assert not closed
        now = 300.0
        callbacks[0](None, 0, 7, 0)
        return windows_folder._CANCELLED

    user = SimpleNamespace(GetForegroundWindow=lambda: 123, GetClassNameW=class_name, SetTimer=set_timer,
                           KillTimer=lambda _, timer: stopped.append(timer))
    monkeypatch.setattr(ct, "WinDLL", lambda *_args, **_kwargs: user, raising=False)
    if not hasattr(ct, "WINFUNCTYPE"):
        monkeypatch.setattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE, raising=False)
    monkeypatch.setattr(windows_folder, "monotonic", lambda: now)
    monkeypatch.setattr(windows_folder, "_method", lambda _, slot, *_types: (
        show if slot == 3 else lambda _, result: closed.append(result)
    ))

    try:
        assert windows_folder._show(dialog) == windows_folder._CANCELLED
        assert closed == [windows_folder._CANCELLED] and stopped == [7]
        callbacks[0](None, 0, 7, 0)
        assert closed == [windows_folder._CANCELLED]
    finally:
        windows_folder._timer_callback.cache_clear()


def test_route_uses_windows_picker_on_windows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tongdory2.config import Settings
    from tongdory2.web.app import create_app

    home = tmp_path / "home"
    picked = home / "Documents" / "정리"
    picked.mkdir(parents=True)
    app = create_app(Settings(_env_file=None, LINER_API_KEY="", TONGDORY_HOME=str(home), TONGDORY_DATA_DIR=str(tmp_path / "d")))
    monkeypatch.setattr(setup_routes.sys, "platform", "win32")
    monkeypatch.setattr(setup_routes, "_pick_folders_windows", lambda p, i, m: {"paths": [str(picked)]})
    res = TestClient(app, base_url="http://127.0.0.1:8765").post("/setup/pick-folder", data={"multiple": "true"}).json()
    assert res["paths"] == ["~/Documents/정리"]  # 화면에는 홈 기준 경로로 보여 준다


def test_only_one_dialog_at_a_time(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tongdory2.config import Settings
    from tongdory2.web.app import create_app

    app = create_app(Settings(_env_file=None, LINER_API_KEY="", TONGDORY_HOME=str(tmp_path), TONGDORY_DATA_DIR=str(tmp_path / "d")))
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert setup_routes._PICK_LOCK.acquire(blocking=False)  # 다른 버튼에서 선택 창이 열려 있는 상태
    try:
        assert client.post("/setup/pick-folder").json() == {"cancelled": True, "paths": []}
    finally:
        setup_routes._PICK_LOCK.release()
    assert client.post("/setup/pick-folder").json()["fallback"] is True  # 닫힌 뒤에는 다시 열 수 있다(테스트 중이라 웹 팝업)
