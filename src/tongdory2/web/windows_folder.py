"""Windows 탐색기 형식의 폴더 선택 창. Win32 COM을 호출하는 스레드에서 열고 닫는다."""

from __future__ import annotations

import ctypes as ct
from contextlib import contextmanager
from ctypes import wintypes as win
from functools import lru_cache
from time import monotonic
from uuid import UUID


class _GUID(ct.Structure):
    _fields_ = [("data1", ct.c_uint32), ("data2", ct.c_uint16), ("data3", ct.c_uint16), ("data4", ct.c_uint8 * 8)]


_FILE_OPEN_DIALOG = _GUID.from_buffer_copy(UUID("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7").bytes_le)
_I_FILE_OPEN_DIALOG = _GUID.from_buffer_copy(UUID("d57c7288-d4ad-4768-be02-9d969532d960").bytes_le)
_I_SHELL_ITEM = _GUID.from_buffer_copy(UUID("43826d1e-e718-42ee-bc55-a1e261c37bfe").bytes_le)
_HRESULT = ct.c_int32
_CANCELLED = _HRESULT(0x800704C7).value  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
_PATH_OPTIONS = 0x08 | 0x20 | 0x40 | 0x800  # NOCHANGEDIR, PICKFOLDERS, FORCEFILESYSTEM, PATHMUSTEXIST
_ALLOW_MULTIPLE = 0x200
_FILE_SYSTEM_PATH = 0x80058000  # SIGDN_FILESYSPATH
_TIMEOUT_MS = 300_000

# Windows SDK의 COM 메서드 위치. 상속된 IUnknown·IModalWindow 메서드부터 센다.
_RELEASE, _SHOW = 2, 3
_SET_OPTIONS, _GET_OPTIONS, _SET_DEFAULT_FOLDER, _SET_TITLE, _CLOSE, _GET_RESULTS = 9, 10, 11, 17, 23, 27
_GET_DISPLAY_NAME = 5  # IShellItem
_GET_COUNT, _GET_ITEM_AT = 7, 8  # IShellItemArray


def _checked(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} (HRESULT 0x{result & 0xFFFFFFFF:08X})")


def _method(interface, index, *argtypes, result_type=_HRESULT):
    address = ct.cast(interface, ct.POINTER(ct.POINTER(ct.c_void_p)))[0][index]
    return ct.WINFUNCTYPE(result_type, ct.c_void_p, *argtypes)(address)


@contextmanager
def _com_pointer():
    pointer = ct.c_void_p()
    try:
        yield pointer
    finally:
        if pointer.value is not None:
            _method(pointer, _RELEASE, result_type=win.ULONG)(pointer)


def _folder_results(dialog, free_memory) -> list[str]:
    paths = []
    with _com_pointer() as items:
        _checked(_method(dialog, _GET_RESULTS, ct.POINTER(ct.c_void_p))(dialog, ct.byref(items)), "GetResults")
        count = win.DWORD()
        _checked(_method(items, _GET_COUNT, ct.POINTER(win.DWORD))(items, ct.byref(count)), "GetCount")
        for index in range(count.value):
            with _com_pointer() as item:
                _checked(_method(items, _GET_ITEM_AT, win.DWORD, ct.POINTER(ct.c_void_p))(
                    items, index, ct.byref(item)), "GetItemAt")
                address = ct.c_void_p()
                _checked(_method(item, _GET_DISPLAY_NAME, ct.c_uint32, ct.POINTER(ct.c_void_p))(
                    item, _FILE_SYSTEM_PATH, ct.byref(address)), "GetDisplayName")
                try:
                    if not address:
                        raise OSError("GetDisplayName did not return a path")
                    paths.append(ct.wstring_at(address))
                finally:
                    free_memory(address)
    return paths


_timers: dict[int, tuple[ct.c_void_p, float]] = {}


@lru_cache(maxsize=1)
def _timer_callback():
    # KillTimer 뒤에도 처리 대기 중인 메시지가 있을 수 있으므로 콜백을 계속 보관한다.
    @ct.WINFUNCTYPE(None, win.HWND, win.UINT, ct.c_size_t, win.DWORD)
    def close_after_timeout(_hwnd, _message, timer_id, _ticks):
        active = _timers.get(timer_id)
        if active and monotonic() >= active[1]:
            dialog = active[0]
            _method(dialog, _CLOSE, _HRESULT)(dialog, _CANCELLED)

    return close_after_timeout


def _show(dialog) -> int:
    user = ct.WinDLL("user32.dll", use_last_error=True)
    user.GetForegroundWindow.restype = win.HWND
    user.GetClassNameW.argtypes = (win.HWND, win.LPWSTR, ct.c_int)
    user.GetClassNameW.restype = ct.c_int
    owner = user.GetForegroundWindow()
    class_name = ct.create_unicode_buffer(64)
    if owner:
        user.GetClassNameW(owner, class_name, len(class_name))
    # 요청 직후 브라우저가 전면에 있으면 그 창을 기준으로 선택창을 띄운다.
    if class_name.value not in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
        owner = None

    callback = _timer_callback()
    user.SetTimer.argtypes = (win.HWND, ct.c_size_t, win.UINT, type(callback))
    user.SetTimer.restype = ct.c_size_t
    user.KillTimer.argtypes = (win.HWND, ct.c_size_t)
    timer = user.SetTimer(None, 0, _TIMEOUT_MS, callback)
    if not timer:
        raise ct.WinError(ct.get_last_error())
    # 타이머 ID가 재사용돼도 이전 창의 메시지가 새 창을 닫지 않도록 시간을 확인한다.
    _timers[timer] = (dialog, monotonic() + _TIMEOUT_MS / 1000 - 1)
    try:
        return _method(dialog, _SHOW, win.HWND)(dialog, owner)
    finally:
        _timers.pop(timer, None)
        user.KillTimer(None, timer)


def choose_folders(prompt: str, initial: str, multiple: bool) -> list[str] | None:
    """실제 폴더의 유니코드 경로를 받는다. 취소 시 None, 호출 실패 시 OSError."""
    ole = ct.WinDLL("ole32.dll")
    shell = ct.WinDLL("shell32.dll")
    guid_ref = ct.POINTER(_GUID)
    pointer_ref = ct.POINTER(ct.c_void_p)
    ole.CoInitializeEx.argtypes = (ct.c_void_p, win.DWORD)
    ole.CoInitializeEx.restype = _HRESULT
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = (guid_ref, ct.c_void_p, win.DWORD, guid_ref, pointer_ref)
    ole.CoCreateInstance.restype = _HRESULT
    ole.CoTaskMemFree.argtypes = (ct.c_void_p,)
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = (win.LPCWSTR, ct.c_void_p, guid_ref, pointer_ref)
    shell.SHCreateItemFromParsingName.restype = _HRESULT

    _checked(ole.CoInitializeEx(None, 2), "CoInitializeEx")  # COINIT_APARTMENTTHREADED
    try:
        with _com_pointer() as dialog:
            _checked(ole.CoCreateInstance(ct.byref(_FILE_OPEN_DIALOG), None, 1, ct.byref(_I_FILE_OPEN_DIALOG),
                                          ct.byref(dialog)), "CoCreateInstance")
            options = win.DWORD()
            _checked(_method(dialog, _GET_OPTIONS, ct.POINTER(win.DWORD))(dialog, ct.byref(options)), "GetOptions")
            flags = options.value | _PATH_OPTIONS | (_ALLOW_MULTIPLE if multiple else 0)
            _checked(_method(dialog, _SET_OPTIONS, win.DWORD)(dialog, flags), "SetOptions")
            _checked(_method(dialog, _SET_TITLE, win.LPCWSTR)(dialog, prompt), "SetTitle")
            if initial:
                with _com_pointer() as folder:
                    if shell.SHCreateItemFromParsingName(initial, None, ct.byref(_I_SHELL_ITEM), ct.byref(folder)) >= 0:
                        _method(dialog, _SET_DEFAULT_FOLDER, ct.c_void_p)(dialog, folder)
            result = _show(dialog)
            if result == _CANCELLED:
                return None
            _checked(result, "Show")
            return _folder_results(dialog, ole.CoTaskMemFree)
    finally:
        ole.CoUninitialize()
