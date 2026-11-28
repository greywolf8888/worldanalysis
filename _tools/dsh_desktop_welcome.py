"""Choose the signed DSH desktop's optional local setup actions on startup."""

import ctypes
from ctypes import wintypes
import sys
import time
import unicodedata
from contextlib import ExitStack
from pathlib import Path

import win32api
import win32con
import win32event
import win32gui
import win32process
import win32ui


TITLE = "DeepSeek Harness"
OPTIONS = frozenset({"添加apikey", "addapikey"})
SKIP = frozenset({"稍后配置", "setuplater"})
OTHER_LABELS = (
    frozenset({"登录", "signin"}),
    frozenset({"保存并继续", "saveandcontinue"}),
    frozenset({"返回登录", "backtosignin"}),
)
USER32 = ctypes.WinDLL("user32", use_last_error=True)
USER32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
USER32.PrintWindow.restype = wintypes.BOOL
USER32.GetDpiForWindow.argtypes = (wintypes.HWND,)
USER32.GetDpiForWindow.restype = wintypes.UINT
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def process_windows(pid):
    found = []

    def add(handle, _):
        if win32gui.IsWindowVisible(handle) and win32process.GetWindowThreadProcessId(handle)[1] == pid:
            found.append(handle)
        return True

    win32gui.EnumWindows(add, None)
    return found


def same_window(win, pid):
    return (win32gui.IsWindow(win) and win32gui.IsWindowVisible(win)
            and win32process.GetWindowThreadProcessId(win)[1] == pid)


def rendered_footer(win, width, height):
    import numpy as np
    from PIL import Image

    with ExitStack() as cleanup:
        raw = win32gui.GetWindowDC(win)
        cleanup.callback(win32gui.ReleaseDC, win, raw)
        source = win32ui.CreateDCFromHandle(raw)
        cleanup.callback(source.DeleteDC)
        memory = source.CreateCompatibleDC()
        cleanup.callback(memory.DeleteDC)
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        cleanup.callback(win32gui.DeleteObject, bitmap.GetHandle())
        selected = memory.SelectObject(bitmap)
        cleanup.callback(memory.SelectObject, selected)
        if not USER32.PrintWindow(win, memory.GetSafeHdc(), 2):
            return None
        image = Image.frombytes("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX")
        left, top = int(width * .16), int(height * .68)
        crop = image.crop((left, top, width - left, int(height * .97)))
        return np.asarray(crop), left, top


def recognize_action(rows):
    labels = {
        "".join(unicodedata.normalize("NFKC", text).casefold().split()): (box, float(confidence))
        for box, text, confidence in rows
    }
    if labels.keys() & OTHER_LABELS[1] and labels.keys() & OTHER_LABELS[2]:
        names, action = SKIP, "skip"
    elif labels.keys() & OTHER_LABELS[0]:
        names, action = OPTIONS, "options"
    else:
        return None
    matches = labels.keys() & names
    if len(matches) != 1:
        return None
    box, confidence = labels[matches.pop()]
    if confidence < .6:
        return None
    return action, round(sum(p[0] for p in box) / 4), round(sum(p[1] for p in box) / 4)


def click_welcome_action(win, pid, rect, point):
    if (not same_window(win, pid) or win32gui.GetWindowRect(win) != rect
            or win32gui.GetWindowText(win) != TITLE or process_windows(pid) != [win]):
        return False
    x, y = win32gui.ScreenToClient(win, point)
    _, _, width, height = win32gui.GetClientRect(win)
    if not (0 <= x < width and 0 <= y < height):
        return False
    location = (y << 16) | (x & 0xFFFF)
    win32gui.PostMessage(win, win32con.WM_MOUSEMOVE, 0, location)
    win32gui.PostMessage(win, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, location)
    win32gui.PostMessage(win, win32con.WM_LBUTTONUP, 0, location)
    return True


def open_workspace(pid, process):
    from rapidocr_onnxruntime import RapidOCR

    deadline = time.monotonic() + 35
    sent = set()
    blocked = False
    recognizer = None
    while time.monotonic() < deadline:
        if win32event.WaitForSingleObject(process, 0) == win32con.WAIT_OBJECT_0:
            return "process_exited"
        windows = process_windows(pid)
        if any(win32gui.GetWindowText(w).endswith("— " + TITLE) for w in windows):
            return "workspace_after_skip" if "skip" in sent else "workspace_ready"
        if len(windows) == 1 and "skip" not in sent:
            win = windows[0]
            left, top, right, bottom = rect = win32gui.GetWindowRect(win)
            scale = (USER32.GetDpiForWindow(win) or 96) / 96
            width, height = right - left, bottom - top
            if win32gui.GetWindowText(win) == TITLE and 570 <= width / scale <= 650 and 670 <= height / scale <= 750:
                if recognizer is None:
                    recognizer = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
                capture = rendered_footer(win, width, height)
                if capture is not None:
                    image, dx, dy = capture
                    rows, _ = recognizer(image, use_cls=False)
                    control = recognize_action(rows or [])
                    if control and control[0] not in sent:
                        action, x, y = control
                        if click_welcome_action(win, pid, rect, (left + dx + x, top + dy + y)):
                            sent.add(action)
                        else:
                            blocked = True
        time.sleep(.3)
    return "welcome_action_unavailable" if blocked else "workspace_not_read_back"


def main():
    if len(sys.argv) != 3:
        return 2
    USER32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    pid = int(sys.argv[1])
    process = win32api.OpenProcess(0x1000 | win32con.SYNCHRONIZE, False, pid)
    try:
        filename = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(filename))
        if not KERNEL32.QueryFullProcessImageNameW(int(process), 0, filename, ctypes.byref(length)):
            print("target_path_unavailable")
            return 2
        if Path(filename.value) != Path(sys.argv[2]):
            print("target_path_mismatch")
            return 2
        status = open_workspace(pid, process)
        print(status)
        return 0 if status in {"workspace_after_skip", "workspace_ready", "process_exited"} else 3
    finally:
        process.Close()


if __name__ == "__main__":
    raise SystemExit(main())
