"""winsessions - find, focus, and gracefully close PowerShell session windows.

Claude Code sessions live in powershell.exe / pwsh.exe consoles whose window
title is the session name, prefixed with a status glyph. This module is the Win32
layer only -- no UI. Run it directly to dump what it can see:

    python winsessions.py
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import traceback

# --- Win32 plumbing -------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetCurrentThreadId.restype = wt.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE, SW_SHOW = 9, 5
HWND_TOPMOST, HWND_NOTOPMOST = wt.HWND(-1), wt.HWND(-2)
SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040

TARGET_PROCS = {"powershell.exe", "pwsh.exe"}
REFRESH_MS = 1500


def _process_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def _window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def split_title(title):
    """Separate a leading status glyph (Claude's spinner) from the session name."""
    title = title.strip()
    if title and not (title[0].isalnum() or title[0] in "._-/:'\"(" + chr(92)):
        return title[0], title[1:].strip()
    return "", title


class Session(object):
    def __init__(self, hwnd, pid, title):
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.glyph, self.name = split_title(title)
        if os.path.isabs(self.name):
            # a plain console titles itself with its full exe path
            self.name = os.path.basename(self.name)

    def key(self):
        return (self.hwnd, self.title)


def list_sessions():
    """Visible powershell/pwsh windows, sorted by session name."""
    found = []
    cache = {}

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = pid.value
        if pid not in cache:
            cache[pid] = _process_name(pid).lower()
        if cache[pid] in TARGET_PROCS:
            found.append(Session(hwnd, pid, title))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    found.sort(key=lambda s: (s.name.lower(), s.pid))
    return found


def focus_window(hwnd):
    """Bring hwnd to the foreground. Returns True if it actually got focus."""
    if not user32.IsWindow(hwnd):
        return False
    user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)

    our_tid = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    fg_tid = 0
    if foreground:
        fg_tid = user32.GetWindowThreadProcessId(foreground, None)
    attached = bool(fg_tid) and fg_tid != our_tid and user32.AttachThreadInput(
        our_tid, fg_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_tid, fg_tid, False)

    if user32.GetForegroundWindow() == hwnd:
        return True

    # Windows ignored the request (common from a background app): toggle the
    # window topmost to force it up, then try once more.
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


# --- graceful close -------------------------------------------------------
#
# Closing a session escalates, gentlest first:
#   1. type "/exit" into its console, so Claude Code exits at the app level
#   2. WM_CLOSE -- exactly what clicking the window's X does (CTRL_CLOSE_EVENT)
#   3. TerminateProcess, only when the caller explicitly asks for it
#
# Steps are separate calls; the GUI drives the timing and checks progress with
# is_alive() in between.

WM_CLOSE = 0x0010
STD_INPUT = -10
KEY_EVENT = 0x0001
VK_RETURN, VK_ESCAPE = 0x0D, 0x1B
CTRL_U = "\x15"          # clears the current input line
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW, OPEN_EXISTING = 0x03, 3
INVALID_HANDLE = wt.HANDLE(-1).value
PROCESS_TERMINATE = 0x0001


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wt.BOOL),
                ("wRepeatCount", wt.WORD),
                ("wVirtualKeyCode", wt.WORD),
                ("wVirtualScanCode", wt.WORD),
                ("UnicodeChar", wt.WCHAR),
                ("dwControlKeyState", wt.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]
    _fields_ = [("EventType", wt.WORD), ("Event", _U)]


user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.AttachConsole.argtypes = [wt.DWORD]
kernel32.FreeConsole.restype = wt.BOOL
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.WriteConsoleInputW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]


def _key_records(text, vk=0):
    """Key-down/key-up INPUT_RECORDs spelling out text."""
    records = []
    for char in text:
        for down in (True, False):
            record = INPUT_RECORD()
            record.EventType = KEY_EVENT
            record.Event.KeyEvent.bKeyDown = down
            record.Event.KeyEvent.wRepeatCount = 1
            record.Event.KeyEvent.wVirtualKeyCode = vk
            record.Event.KeyEvent.UnicodeChar = char
            records.append(record)
    return records


def send_console_keys(pid, chunks):
    """Type into another process's console. chunks is [(text, virtual-key), ...].

    Works only from a process with no console of its own (pythonw), because we
    borrow the target's console for the duration of the write.
    """
    records = []
    for text, vk in chunks:
        records.extend(_key_records(text, vk))
    if not records:
        return False

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return False
    handle = INVALID_HANDLE
    try:
        # pythonw has no valid std handles, so open the console input buffer.
        handle = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE:
            return False
        buf = (INPUT_RECORD * len(records))(*records)
        written = wt.DWORD()
        return bool(kernel32.WriteConsoleInputW(handle, buf, len(records),
                                                ctypes.byref(written)))
    finally:
        if handle != INVALID_HANDLE:
            kernel32.CloseHandle(handle)
        kernel32.FreeConsole()


def send_exit_command(pid):
    """Clear whatever is on the prompt, then run /exit in the session.

    Ctrl+U (not Escape) does the clearing: it empties a half-typed prompt
    without interrupting a response Claude is still streaming.
    """
    if not send_console_keys(pid, [(CTRL_U, 0)]):
        return False
    return send_console_keys(pid, [("/exit", 0), ("\r", VK_RETURN)])


def request_close(hwnd):
    """Ask the window to close -- the same signal as clicking its X."""
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def terminate(pid):
    """Last resort. Kills the process outright; nothing gets to clean up."""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def is_alive(hwnd):
    return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))




# --- foreground/background helpers ---------------------------------------

SW_MINIMIZE = 6


def minimize(hwnd):
    """Send a window to the taskbar (our notion of 'backgrounded')."""
    return bool(user32.ShowWindow(hwnd, SW_MINIMIZE))


def is_minimized(hwnd):
    return bool(user32.IsIconic(hwnd))


def foreground_hwnd():
    return user32.GetForegroundWindow()


if __name__ == "__main__":
    for item in list_sessions():
        line = "  pid %-6d hwnd %-11d %s" % (item.pid, item.hwnd, item.title)
        print(line.encode("ascii", "replace").decode())


# --- global hotkey --------------------------------------------------------
#
# RegisterHotKey with a NULL window posts WM_HOTKEY to the *thread* queue, and
# Tk's own message pump would eat those before we ever saw them. So the hotkey
# lives on its own thread with its own GetMessage loop, and hands presses back
# through the callback.

import threading

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK_SPACE = 0x20
GA_ROOT = 2
_HOTKEY_ID = 1

user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]


def root_window(hwnd):
    """The real top-level window for an hwnd (Tk nests its toplevels)."""
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


class HotkeyThread(threading.Thread):
    """Owns one system-wide hotkey and calls on_press (on this thread) for it.

    registered() blocks briefly and reports whether Windows gave us the combo;
    another app may already own it, in which case nothing is listening.
    """

    def __init__(self, modifiers, vk, on_press):
        threading.Thread.__init__(self, daemon=True)
        self.modifiers = modifiers
        self.vk = vk
        self.on_press = on_press
        self.ok = False
        self.thread_id = 0
        self._ready = threading.Event()

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.ok = bool(user32.RegisterHotKey(None, _HOTKEY_ID, self.modifiers, self.vk))
        self._ready.set()
        if not self.ok:
            return
        try:
            message = wt.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:
                        # a broken callback must not take the hotkey down with it
                        traceback.print_exc()
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)

    def registered(self, timeout=2.0):
        self._ready.wait(timeout)
        return self.ok

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
