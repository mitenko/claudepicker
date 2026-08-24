"""claudepicker - a window listing every running Claude Code / PowerShell session.

Click a session to bring it forward; click the one that is already in front to
send it back to the taskbar. The X on a row closes the session, gracefully:
Ctrl+U then "/exit" typed into it, falling back to WM_CLOSE, and only forcing a
terminate if you ask after it refuses to die.

    pythonw claudepicker.py     run it (no console window of its own)

Win32 details live in winsessions.py.
"""

import os
import queue
import sys
import traceback
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winsessions as ws

# the one place to change the global hotkey
HOTKEY_LABEL = "Ctrl+Alt+Space"
HOTKEY_MODS = ws.MOD_CONTROL | ws.MOD_ALT | ws.MOD_NOREPEAT
HOTKEY_VK = ws.VK_SPACE
HOTKEY_POLL_MS = 75

REFRESH_MS = 1500
TICKS_BEFORE_WM_CLOSE = 4     # ~6s of typing /exit before we knock on the window
TICKS_BEFORE_STUCK = 9        # ~13s before we admit it is not closing

BG = "#1b1b1f"
ROW = "#26262b"
ROW_HOVER = "#32323a"
ROW_SEL = "#0f4f7a"
FG = "#e6e6e6"
DIM = "#8a8a92"
ACCENT = "#d97757"
WARN = "#e0b341"
BAD = "#8b2e2e"

F_TITLE = ("Segoe UI", 12, "bold")
F_NAME = ("Segoe UI", 11, "bold")
F_GLYPH = ("Segoe UI Symbol", 12)
F_SMALL = ("Segoe UI", 8)
F_BTN = ("Segoe UI", 9, "bold")


class Row(object):
    """One session line.

    Widgets are created once and reused across refreshes so the list never
    flickers while Claude's spinner glyph animates. A row is in one of four
    states: normal, confirm (asking whether to close), closing (exit in
    progress) and stuck (graceful close failed, offering force).
    """

    def __init__(self, app, session):
        self.app = app
        self.hwnd = session.hwnd
        self.pid = session.pid
        self.name = session.name
        self.state = "normal"
        self.hovered = False

        self.frame = tk.Frame(app.body, bg=ROW)

        self.main = tk.Frame(self.frame, bg=ROW, padx=8, pady=6)
        self.num = tk.Label(self.main, bg=ROW, fg=DIM, font=F_SMALL, width=2)
        self.glyph = tk.Label(self.main, bg=ROW, fg=ACCENT, font=F_GLYPH, width=2)
        self.label = tk.Label(self.main, bg=ROW, fg=FG, font=F_NAME, anchor="w")
        self.note = tk.Label(self.main, bg=ROW, fg=DIM, font=F_SMALL, anchor="e")
        self.close_btn = tk.Label(self.main, bg=ROW, fg="#5c5c64", font=F_BTN,
                                  text="✕", padx=6, cursor="hand2")
        self.num.pack(side="left")
        self.glyph.pack(side="left")
        self.label.pack(side="left", fill="x", expand=True)
        self.close_btn.pack(side="right")
        self.note.pack(side="right")

        self.ask = tk.Frame(self.frame, bg=ROW, padx=8, pady=6)
        self.ask_label = tk.Label(self.ask, bg=ROW, fg=WARN, font=F_NAME, anchor="w")
        self.yes = tk.Label(self.ask, bg=BAD, fg=FG, font=F_BTN, text=" close ",
                            padx=6, pady=2, cursor="hand2")
        self.no = tk.Label(self.ask, bg="#3a3a42", fg=FG, font=F_BTN, text=" cancel ",
                           padx=6, pady=2, cursor="hand2")
        self.ask_label.pack(side="left", fill="x", expand=True)
        self.no.pack(side="right", padx=(6, 0))
        self.yes.pack(side="right")

        self.main.pack(fill="x")
        for widget in (self.frame, self.main, self.num, self.glyph, self.label,
                       self.note):
            widget.bind("<Button-1>", lambda e: self.app.activate(self.hwnd))
            widget.bind("<Enter>", self.on_enter)
            widget.bind("<Leave>", self.on_leave)
        self.close_btn.bind("<Button-1>", self.on_close_click)
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.configure(fg=ACCENT))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.configure(fg="#5c5c64"))
        self.yes.bind("<Button-1>", lambda e: self.app.begin_close(self.hwnd))
        self.no.bind("<Button-1>", lambda e: self.set_state("normal"))

        self.update(session, 0)

    # --- appearance -------------------------------------------------------

    def tinted(self):
        return (self.frame, self.main, self.num, self.glyph, self.label, self.note,
                self.close_btn, self.ask, self.ask_label)

    def paint(self, color):
        for widget in self.tinted():
            widget.configure(bg=color)

    def refresh_paint(self):
        if self.state in ("confirm", "closing", "stuck"):
            self.paint("#2b2229")
        elif self.app.selected == self.hwnd:
            self.paint(ROW_SEL)
        elif self.hovered:
            self.paint(ROW_HOVER)
        else:
            self.paint(ROW)

    def update(self, session, index):
        """Refresh from a fresh Session and (re)position at the given index."""
        self.name = session.name
        self.frame.grid(row=index, column=0, sticky="ew", pady=1)
        if self.state == "normal":
            self.num.configure(text=str(index + 1) if index < 9 else "")
            self.glyph.configure(text=session.glyph or "•")
            self.label.configure(text=session.name or "(untitled)", fg=FG)
            front = self.app.front == self.hwnd and not ws.is_minimized(self.hwnd)
            self.note.configure(text="in front" if front else "pid %d" % session.pid,
                                fg=ACCENT if front else DIM)
        self.refresh_paint()

    def set_state(self, state):
        self.state = state
        if state == "confirm":
            self.main.pack_forget()
            self.ask_label.configure(text="Close %s?" % self.name)
            self.ask.pack(fill="x")
        else:
            self.ask.pack_forget()
            self.main.pack(fill="x")
        if state == "closing":
            self.label.configure(text=self.name, fg=DIM)
            self.note.configure(text="closing...", fg=WARN)
            self.close_btn.pack_forget()
        elif state == "stuck":
            self.label.configure(text=self.name, fg=DIM)
            self.note.configure(text="won't close - click to force", fg=ACCENT)
            self.close_btn.pack_forget()
        elif state == "normal":
            self.close_btn.pack(side="right")
        self.refresh_paint()

    # --- events -----------------------------------------------------------

    def on_enter(self, _event):
        self.hovered = True
        self.refresh_paint()

    def on_leave(self, _event):
        self.hovered = False
        self.refresh_paint()

    def on_close_click(self, event):
        self.app.ask_close(self.hwnd)
        return "break"          # don't also focus the window

    def flash_failure(self):
        self.paint(BAD)
        self.frame.after(400, self.refresh_paint)

    def destroy(self):
        self.frame.destroy()


class PickerApp(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Claude Picker")
        self.geometry("400x460")
        self.minsize(320, 200)
        self.configure(bg=BG)

        header = tk.Frame(self, bg=BG, padx=10, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="Claude sessions", bg=BG, fg=FG,
                 font=F_TITLE).pack(side="left")
        self.count = tk.Label(header, text="", bg=BG, fg=DIM, font=F_SMALL)
        self.count.pack(side="right")

        self.body = tk.Frame(self, bg=BG, padx=8)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.empty = tk.Label(self.body, text="No PowerShell sessions found.",
                              bg=BG, fg=DIM, font=("Segoe UI", 10), pady=20)

        footer = tk.Frame(self, bg=BG, padx=10, pady=6)
        footer.pack(fill="x")
        self.on_top = tk.BooleanVar(value=True)
        tk.Checkbutton(footer, text="Always on top", variable=self.on_top,
                       command=self.apply_on_top, bg=BG, fg=DIM, font=F_SMALL,
                       activebackground=BG, activeforeground=FG, selectcolor=ROW,
                       borderwidth=0, highlightthickness=0).pack(side="left")
        self.status = tk.Label(footer, text="", bg=BG, fg=DIM, font=F_SMALL,
                               anchor="e")
        self.status.pack(side="right")
        tk.Label(self, bg=BG, fg="#5c5c64", font=("Segoe UI", 7), pady=4,
                 text="click to focus / click again to hide  ·  1-9 jump  ·  "
                      "Del close  ·  F5 refresh  ·  %s show/hide"
                      % HOTKEY_LABEL).pack(fill="x")

        self.rows = {}
        self.order = []
        self.selected = None
        self.front = None          # session we believe is in the foreground
        self.closing = {}          # hwnd -> ticks since close started
        self.apply_on_top()

        self.bind("<Up>", lambda e: self.move(-1))
        self.bind("<Down>", lambda e: self.move(1))
        self.bind("<Return>", self.on_return)
        self.bind("<Delete>", lambda e: self.ask_close(self.selected))
        self.bind("<F5>", lambda e: self.refresh())
        self.bind("<Escape>", self.on_escape)
        self.bind("<Key>", self.on_key)

        self.hotkey_presses = queue.Queue()
        self.hotkey = ws.HotkeyThread(HOTKEY_MODS, HOTKEY_VK,
                                      lambda: self.hotkey_presses.put(True))
        self.hotkey.start()
        if not self.hotkey.registered(1.0):
            self.say("%s is already taken - hotkey off" % HOTKEY_LABEL, ACCENT)
        self.after(HOTKEY_POLL_MS, self.poll_hotkey)

        self.refresh()

    def destroy(self):
        self.hotkey.stop()
        tk.Tk.destroy(self)

    # --- global hotkey ----------------------------------------------------

    def poll_hotkey(self):
        """Presses arrive on the hotkey thread; act on them from the UI thread."""
        pressed = False
        try:
            while True:
                self.hotkey_presses.get_nowait()
                pressed = True
        except queue.Empty:
            pass
        if pressed:
            self.toggle_self()
        self.after(HOTKEY_POLL_MS, self.poll_hotkey)

    def toggle_self(self):
        """Summon the picker, or dismiss it if it is already the active window."""
        hwnd = ws.root_window(self.winfo_id())
        if self.state() != "normal":
            self.deiconify()
            ws.focus_window(hwnd)
        elif ws.foreground_hwnd() == hwnd:
            self.iconify()
        else:
            ws.focus_window(hwnd)

    # --- chrome -----------------------------------------------------------

    def apply_on_top(self):
        self.attributes("-topmost", bool(self.on_top.get()))

    def say(self, text, color=DIM):
        self.status.configure(text=text, fg=color)

    # --- keyboard ---------------------------------------------------------

    def on_key(self, event):
        if event.char and event.char in "123456789":
            index = int(event.char) - 1
            if index < len(self.order):
                self.activate(self.order[index])

    def on_return(self, _event):
        row = self.rows.get(self.selected)
        if row and row.state == "confirm":
            self.begin_close(self.selected)
        else:
            self.activate(self.selected)

    def on_escape(self, _event):
        pending = [r for r in self.rows.values() if r.state == "confirm"]
        if pending:
            for row in pending:
                row.set_state("normal")
        else:
            self.iconify()

    def move(self, delta):
        if not self.order:
            return
        if self.selected in self.order:
            index = self.order.index(self.selected) + delta
        else:
            index = 0 if delta > 0 else len(self.order) - 1
        self.select(self.order[max(0, min(index, len(self.order) - 1))])

    def select(self, hwnd):
        self.selected = hwnd
        for row in self.rows.values():
            row.refresh_paint()

    # --- focus / unfocus --------------------------------------------------

    def activate(self, hwnd):
        """Bring a session forward, or push it back if it is already in front."""
        row = self.rows.get(hwnd)
        if row is None:
            return
        if row.state == "stuck":
            self.force_close(hwnd)
            return
        if row.state != "normal":
            return
        self.select(hwnd)

        if self.front == hwnd and not ws.is_minimized(hwnd):
            if ws.minimize(hwnd):
                self.front = None
                self.say("sent %s to the taskbar" % row.name)
            else:
                row.flash_failure()
                self.say("could not hide %s" % row.name, ACCENT)
        elif ws.focus_window(hwnd):
            self.front = hwnd
            self.say("focused %s" % row.name)
        else:
            row.flash_failure()
            self.say("could not focus %s" % row.name, ACCENT)
        self.repaint_notes()

    def repaint_notes(self):
        for hwnd, row in self.rows.items():
            if row.state == "normal":
                front = self.front == hwnd and not ws.is_minimized(hwnd)
                row.note.configure(
                    text="in front" if front else "pid %d" % row.pid,
                    fg=ACCENT if front else DIM)

    # --- closing ----------------------------------------------------------

    def ask_close(self, hwnd):
        row = self.rows.get(hwnd)
        if row and row.state == "normal":
            for other in self.rows.values():
                if other.state == "confirm":
                    other.set_state("normal")
            row.set_state("confirm")
            self.select(hwnd)

    def begin_close(self, hwnd):
        row = self.rows.get(hwnd)
        if row is None:
            return
        row.set_state("closing")
        self.closing[hwnd] = 0
        if ws.send_exit_command(row.pid):
            self.say("typed /exit into %s" % row.name, WARN)
        else:
            ws.request_close(hwnd)
            self.closing[hwnd] = TICKS_BEFORE_WM_CLOSE
            self.say("asked %s to close" % row.name, WARN)

    def force_close(self, hwnd):
        row = self.rows.get(hwnd)
        if row is None:
            return
        if ws.terminate(row.pid):
            self.say("terminated %s" % row.name, ACCENT)
        else:
            row.flash_failure()
            self.say("could not terminate %s" % row.name, ACCENT)

    def tick_closing(self):
        """Escalate any close in progress: /exit, then WM_CLOSE, then give up."""
        for hwnd in list(self.closing):
            row = self.rows.get(hwnd)
            if row is None:                       # window gone: it worked
                self.closing.pop(hwnd)
                continue
            self.closing[hwnd] += 1
            ticks = self.closing[hwnd]
            if ticks == TICKS_BEFORE_WM_CLOSE:
                ws.request_close(hwnd)
                self.say("/exit did not take - asked %s to close" % row.name, WARN)
            elif ticks >= TICKS_BEFORE_STUCK:
                self.closing.pop(hwnd)
                row.set_state("stuck")
                self.say("%s will not close" % row.name, ACCENT)

    # --- refresh ----------------------------------------------------------

    def refresh(self):
        sessions = ws.list_sessions()
        live = set(s.hwnd for s in sessions)
        for hwnd in [h for h in self.rows if h not in live]:
            row = self.rows.pop(hwnd)
            if row.state in ("closing", "stuck"):
                self.say("closed %s" % row.name)
            row.destroy()

        for index, session in enumerate(sessions):
            if session.hwnd not in self.rows:
                self.rows[session.hwnd] = Row(self, session)
            self.rows[session.hwnd].update(session, index)
        self.order = [s.hwnd for s in sessions]

        foreground = ws.foreground_hwnd()
        if foreground in self.rows:
            self.front = foreground
        elif self.front is not None and self.front not in self.rows:
            self.front = None
        self.repaint_notes()

        if self.selected not in self.rows:
            self.selected = self.order[0] if self.order else None
        for row in self.rows.values():
            row.refresh_paint()

        if sessions:
            self.empty.grid_forget()
        else:
            self.empty.grid(row=0, column=0)
        self.count.configure(text="%d session%s"
                                  % (len(sessions), "" if len(sessions) == 1 else "s"))

        self.tick_closing()
        self.after(REFRESH_MS, self.refresh)


def main():
    PickerApp().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "error.txt"), "w") as handle:
            handle.write(traceback.format_exc())
        raise SystemExit(1)
