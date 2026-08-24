#!/usr/bin/env python3
"""Small Tkinter front-end for r34dl.

Launch it directly (`python r34gui.py`) or by running `python r34dl.py` with
no arguments. All the actual work happens in r34dl.run_download; this module
only collects settings, runs the download on a worker thread, and pumps
messages back to the UI thread through a queue.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import r34dl
from r34dl import DEFAULT_RATE, Options, Progress

PAD = 8


class UndoHistory:
    """Ctrl+Z / Ctrl+Y for a Tk Entry, which has no undo of its own.

    Only the Text widget supports -undo; Entry has nothing bound at all, so
    this snapshots the variable on every change. Keystrokes closer together
    than COALESCE collapse into a single step, so undo moves by typing runs
    rather than one letter at a time.
    """

    COALESCE = 0.7   # seconds
    LIMIT = 200      # snapshots kept per field

    def __init__(self, entry: ttk.Entry, var: tk.StringVar):
        self.entry = entry
        self.var = var
        self.current = var.get()
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.last_change = 0.0
        self.suspend = False

        var.trace_add("write", self._on_change)
        entry.bind("<Control-z>", self.undo)
        entry.bind("<Control-y>", self.redo)
        entry.bind("<Control-Z>", self.redo)   # Ctrl+Shift+Z

    def _on_change(self, *_args) -> None:
        if self.suspend:
            return
        new = self.var.get()
        if new == self.current:
            return
        now = time.monotonic()
        # A single-character difference soon after the last one is the same
        # typing run; anything bigger (a paste, a clear) starts a new step.
        continues_run = (self.undo_stack
                         and now - self.last_change < self.COALESCE
                         and abs(len(new) - len(self.current)) <= 1)
        if not continues_run:
            self.undo_stack.append(self.current)
            del self.undo_stack[:-self.LIMIT]
        self.last_change = now
        self.redo_stack.clear()
        self.current = new

    def _apply(self, value: str) -> None:
        self.suspend = True
        try:
            self.var.set(value)
            self.current = value
            self.entry.icursor("end")
            self.entry.selection_clear()
        finally:
            self.suspend = False
        # Don't let the next keystroke coalesce into the restored value.
        self.last_change = 0.0

    def undo(self, _event=None) -> str:
        if self.undo_stack:
            self.redo_stack.append(self.current)
            self._apply(self.undo_stack.pop())
        return "break"

    def redo(self, _event=None) -> str:
        if self.redo_stack:
            self.undo_stack.append(self.current)
            self._apply(self.redo_stack.pop())
        return "break"


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=PAD)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        # Worker plumbing: the thread posts ("log"|"progress"|"done", payload)
        # tuples here and the UI drains them on a timer.
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.total: int | None = None
        # Tracked here rather than read back from the widget: ttk returns
        # `bar["mode"]` as a Tcl index object that never compares equal to the
        # string "determinate".
        self.determinate = False
        self.done = 0
        self.counts = {"saved": 0, "skipped": 0, "failed": 0}
        self.throttle_left = 0.0
        # ETA as last reported, plus when it arrived, so the display can tick
        # down between updates instead of freezing during a rate-limit pause.
        self.eta: float | None = None
        self.eta_at = 0.0
        self.running = False
        self.undo_histories: list[UndoHistory] = []

        saved_key, saved_user = r34dl.load_credentials()
        self.var_user = tk.StringVar(value=saved_user)
        self.var_key = tk.StringVar(value=saved_key)
        self.var_tags = tk.StringVar()
        # Last folder actually downloaded to; falls back to one beside the
        # script/exe, since the cwd is arbitrary when launched by double-click.
        self.var_out = tk.StringVar(
            value=r34dl.last_folder() or str(r34dl.SCRIPT_DIR / "downloads"))
        self.var_limit = tk.StringVar(value="0")  # 0 = every match
        self.var_workers = tk.StringVar(value="4")
        self.var_rate = tk.StringVar(value=str(DEFAULT_RATE))
        self.var_remember = tk.BooleanVar(value=bool(saved_key and saved_user))
        self.var_verify = tk.BooleanVar(value=True)
        self.var_metadata = tk.BooleanVar(value=False)
        self.var_dryrun = tk.BooleanVar(value=False)
        self.var_showkey = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="Ready.")

        self._build()
        self.after(100, self._drain_events)
        self.after(1000, self._tick)

    # -- layout --------------------------------------------------------

    def _build(self) -> None:
        row = 0

        ttk.Label(self, text="User ID").grid(row=row, column=0, sticky="w", pady=2)
        self._entry(self, self.var_user, width=44).grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=2)
        row += 1

        ttk.Label(self, text="API key").grid(row=row, column=0, sticky="w", pady=2)
        self.entry_key = self._entry(self, self.var_key, show="•")
        self.entry_key.grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Checkbutton(self, text="Show", variable=self.var_showkey,
                        command=self._toggle_key).grid(row=row, column=2,
                                                       sticky="w", padx=(6, 0))
        row += 1

        ttk.Label(self, text="Tags").grid(row=row, column=0, sticky="w", pady=2)
        tags_entry = self._entry(self, self.var_tags)
        tags_entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
        tags_entry.bind("<Return>", lambda _e: self._start())
        tags_entry.focus_set()
        row += 1

        ttk.Label(self, text="(space separated, '-tag' excludes, "
                             "meta-tags like sort:score:desc work)",
                  foreground="gray").grid(row=row, column=1, columnspan=2,
                                          sticky="w", pady=(0, 4))
        row += 1

        ttk.Label(self, text="Save to").grid(row=row, column=0, sticky="w", pady=2)
        # Editable combobox: type a path, or pick one used recently.
        self.combo_out = ttk.Combobox(self, textvariable=self.var_out,
                                      values=r34dl.recent_folders())
        self.undo_histories.append(UndoHistory(self.combo_out, self.var_out))
        self.combo_out.grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Button(self, text="Browse...", command=self._browse).grid(
            row=row, column=2, sticky="ew", padx=(6, 0))
        row += 1

        nums = ttk.Frame(self)
        nums.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        ttk.Label(nums, text="Max posts").pack(side="left")
        self._entry(nums, self.var_limit, width=8).pack(side="left", padx=(4, 2))
        ttk.Label(nums, text="(0 = all)", foreground="gray").pack(side="left",
                                                                  padx=(0, 12))
        ttk.Label(nums, text="Workers").pack(side="left")
        self._entry(nums, self.var_workers, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(nums, text="Req/min").pack(side="left")
        self._entry(nums, self.var_rate, width=5).pack(side="left", padx=(4, 0))
        ttk.Label(nums, text="(site cap: 60)", foreground="gray").pack(
            side="left", padx=(6, 0))
        row += 1

        opts = ttk.Frame(self)
        opts.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(2, 6))
        ttk.Checkbutton(opts, text="Remember credentials",
                        variable=self.var_remember).pack(side="left")
        ttk.Checkbutton(opts, text="Check MD5",
                        variable=self.var_verify).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(opts, text="Save metadata",
                        variable=self.var_metadata).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(opts, text="Dry run",
                        variable=self.var_dryrun).pack(side="left", padx=(12, 0))
        row += 1

        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.btn_start = ttk.Button(buttons, text="Download", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(buttons, text="Cancel", command=self._cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Open folder", command=self._open_folder).pack(
            side="left", padx=(6, 0))
        row += 1

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        row += 1

        ttk.Label(self, textvariable=self.var_status).grid(
            row=row, column=0, columnspan=3, sticky="w")
        row += 1

        log_frame = ttk.Frame(self)
        log_frame.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        self.rowconfigure(row, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=12, wrap="none", state="disabled",
                               font=("Consolas", 9))
        self.log_box.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log_box.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scroll.set)

    def _entry(self, parent, var: tk.StringVar, **kw) -> ttk.Entry:
        """An Entry with undo/redo attached - Tk gives Entry neither."""
        entry = ttk.Entry(parent, textvariable=var, **kw)
        # Kept referenced so the trace and bindings outlive this call.
        self.undo_histories.append(UndoHistory(entry, var))
        return entry

    def _toggle_key(self) -> None:
        self.entry_key.configure(show="" if self.var_showkey.get() else "•")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.var_out.get() or ".")
        if chosen:
            self.var_out.set(chosen)

    def _open_folder(self) -> None:
        path = Path(self.var_out.get())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            import os
            os.startfile(path)  # noqa: S606 - opening the user's own output dir
        else:
            import subprocess
            subprocess.Popen(["xdg-open" if sys.platform.startswith("linux")
                              else "open", str(path)])

    # -- logging -------------------------------------------------------

    def _append_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # -- run -----------------------------------------------------------

    def _read_int(self, var: tk.StringVar, label: str, minimum: int = 1,
                  allow_blank: bool = False) -> int | None:
        raw = var.get().strip()
        if not raw and allow_blank:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{label} must be a whole number")
        if value < minimum:
            raise ValueError(f"{label} must be at least {minimum}")
        return value

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            opts = Options(
                tags=self.var_tags.get().strip(),
                api_key=self.var_key.get().strip(),
                user_id=self.var_user.get().strip(),
                out_dir=Path(self.var_out.get().strip() or "downloads"),
                # 0 or blank means "download every match".
                limit=self._read_int(self.var_limit, "Max posts", minimum=0,
                                     allow_blank=True) or None,
                workers=self._read_int(self.var_workers, "Workers"),
                rate=self._read_int(self.var_rate, "Req/min"),
                verify=self.var_verify.get(),
                metadata=self.var_metadata.get(),
                dry_run=self.var_dryrun.get(),
            )
            if not opts.tags:
                raise ValueError("Enter at least one tag")
            if not opts.api_key or not opts.user_id:
                raise ValueError("Enter both your user ID and API key")
        except ValueError as exc:
            messagebox.showerror("Check your settings", str(exc))
            return

        if self.var_remember.get():
            r34dl.save_credentials(opts.api_key, opts.user_id)

        # Remember the folder only once a run really starts, so browsing
        # around without downloading doesn't clutter the list.
        self.combo_out.configure(values=r34dl.remember_folder(opts.out_dir))

        self.cancel = threading.Event()
        self.total = None
        self.throttle_left = 0.0
        self.eta = None
        self.done = 0
        self.counts = {"saved": 0, "skipped": 0, "failed": 0}
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        # Starts indeterminate; swaps to a real bar as soon as the API tells
        # us how many posts matched.
        self.progress.configure(value=0, maximum=100, mode="indeterminate")
        self.determinate = False
        self.progress.start(30)
        self.var_status.set("Searching...")
        self._append_log(f"--- {opts.tags} -> {opts.out_dir} ---")

        self.worker = threading.Thread(target=self._work, args=(opts,), daemon=True)
        self.worker.start()

    def _work(self, opts: Options) -> None:
        """Runs off the UI thread; everything goes back through the queue."""
        try:
            r34dl.run_download(
                opts,
                log=lambda m: self.events.put(("log", m)),
                on_progress=lambda p: self.events.put(("progress", p)),
                on_total=lambda t: self.events.put(("total", t)),
                on_throttle=lambda s: self.events.put(("throttle", s)),
                cancel=self.cancel,
            )
            self.events.put(("done", None))
        except Exception as exc:  # surfaced in the log and a dialog
            self.events.put(("done", f"{type(exc).__name__}: {exc}"))

    def _cancel(self) -> None:
        self.cancel.set()
        self.var_status.set("Cancelling - finishing current downloads...")
        self.btn_cancel.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "total":
                    self._on_total(payload)
                elif kind == "throttle":
                    self._on_throttle(payload)
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_total(self, total: int | None) -> None:
        self.total = total
        if total:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=total, value=0)
            self.determinate = True

    def _on_throttle(self, seconds: float) -> None:
        """Rate-limit feedback, so a long pause doesn't look like a hang."""
        self.throttle_left = seconds
        self._refresh_status()

    def _tick(self) -> None:
        """Once a second, re-render so the countdown keeps moving."""
        if self.running:
            self._refresh_status()
        self.after(1000, self._tick)

    def _on_progress(self, p: Progress) -> None:
        self.done = sum(p.counts.values())
        self.counts = p.counts
        if p.eta is not None:
            self.eta = p.eta
            self.eta_at = time.monotonic()
        if self.determinate:
            self.progress.configure(value=min(self.done, self.total or self.done))
        self._refresh_status()
        if p.status != "skipped":
            self._append_log(f"[{p.status}] {p.detail}")

    def _remaining_eta(self) -> float | None:
        """The last reported ETA, less the time since it was reported."""
        if self.eta is None:
            return None
        return max(0.0, self.eta - (time.monotonic() - self.eta_at))

    def _refresh_status(self) -> None:
        of_total = f" of {self.total}" if self.total else ""
        text = (f"{self.done}{of_total} processed - saved {self.counts['saved']}, "
                f"skipped {self.counts['skipped']}, failed {self.counts['failed']}")
        eta = self._remaining_eta()
        if eta is not None and self.running:
            text += f"  |  ~{r34dl.fmt_duration(eta)} left"
        if self.throttle_left > 0:
            text += f"  |  rate limit: next request in {self.throttle_left:.0f}s"
        self.var_status.set(text)

    def _on_done(self, error: str | None) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.throttle_left = 0.0
        self.running = False
        if error:
            self._append_log(f"[error] {error}")
            self.var_status.set("Failed.")
            messagebox.showerror("Download failed", error)
        else:
            self._refresh_status()
            self.var_status.set(self.var_status.get() + " - finished.")


def launch() -> int:
    root = tk.Tk()
    root.title("rule34.xxx downloader")
    root.minsize(620, 560)
    try:
        # Slightly less dated-looking on Windows.
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    app = App(root)

    def on_close() -> None:
        app.cancel.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(launch())
