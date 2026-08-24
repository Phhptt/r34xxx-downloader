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


class Column:
    """One column of the history table."""

    def __init__(self, key, heading, minsize, weight=0, anchor="w",
                 sort=None, render=None):
        self.key = key
        self.heading = heading
        self.minsize = minsize
        self.weight = weight
        self.anchor = anchor
        # How to order by this column, and how to draw it.
        self.sort = sort or (lambda r: str(r.get(key) or "").lower())
        self.render = render or (lambda r: str(r.get(key) or ""))


def _shorten(text: str, limit: int, keep_tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return ("…" + text[-(limit - 1):]) if keep_tail else text[:limit - 1] + "…"


def _when(run: dict) -> str:
    stamp = run.get("finished_at") or run.get("started_at") or ""
    # Trim to yyyy-mm-ddThh:mm first: substituting the T widens the string,
    # so slicing afterwards would eat the minutes.
    return stamp[:16].replace("T", "  ")


HISTORY_COLUMNS = [
    Column("check", "", 30, anchor="center", sort=lambda r: 0),
    Column("date", "Last run", 132, sort=lambda r: r.get("finished_at")
           or r.get("started_at") or "", render=_when),
    Column("tags", "Tags", 200, weight=3,
           render=lambda r: _shorten(str(r.get("tags") or ""), 48)),
    Column("folder", "Folder", 180, weight=2,
           render=lambda r: _shorten(str(r.get("folder") or ""), 40, keep_tail=True)),
    Column("matched", "Fetched", 78, anchor="e",
           sort=lambda r: r.get("matched") or 0),
    Column("downloaded", "Downloaded", 102, anchor="e",
           sort=lambda r: r.get("downloaded") or 0),
    Column("ok", "Done", 58, anchor="center",
           sort=lambda r: r.get("status") == "completed",
           render=lambda r: "✓" if r.get("status") == "completed" else ""),
]

MIN_COL_WIDTH = 24      # narrowest a dragged column may get
EDITABLE_COLUMNS = ("tags", "folder")   # editable by double-click
DONE_GREEN = "#1a7f37"
# Both row colours are set explicitly: a tk.Label defaults to the system
# button face, which is near enough to any subtle stripe to erase it.
ROW_BG = "#ffffff"
STRIPE_BG = "#eef1f7"


class HistoryPanel(ttk.Frame):
    """Slide-out panel listing past runs, living inside the main window.

    Built from plain widgets rather than a Treeview: Treeview colours text
    per row, not per cell, so the green tick would drag the whole row's
    colour with it. The list is a curated set of queries to re-check, so it
    stays small enough that real widgets are affordable.

    Its width is fixed and geometry propagation is off, so while the window
    animates open the panel keeps its true size and is simply revealed by the
    widening window rather than being squashed and stretched.
    """

    def __init__(self, app: "App", master):
        super().__init__(master, width=self.panel_width())
        self.grid_propagate(False)
        # A hairline so the panel reads as its own region, not more form.
        ttk.Separator(self, orient="vertical").place(
            relx=0, rely=0, relheight=1.0)
        self.root = app.winfo_toplevel()
        self.app = app
        self.runs: list[dict] = []
        self.checks: dict[int, tk.BooleanVar] = {}
        self.sort_key = "date"
        self.sort_desc = True
        self.header_all = tk.BooleanVar(value=False)
        self._editor: dict | None = None

        self._restore_widths()
        self._build()
        self.reload()
        # Labels don't take focus, so clicking one never raises FocusOut on an
        # open editor. Watch clicks application-wide instead; the handler is a
        # no-op unless an edit is actually in progress.
        self.bind_all("<Button-1>", self._click_outside, add="+")

    @staticmethod
    def _restore_widths() -> None:
        """Re-apply column widths the user dragged in a previous session."""
        saved = r34dl.column_widths()
        for col in HISTORY_COLUMNS:
            if col.key in saved:
                col.minsize = max(MIN_COL_WIDTH, saved[col.key])
                col.weight = 0        # a saved width means it was pinned

    @staticmethod
    def panel_width() -> int:
        """Width at which every column is still fully readable."""
        return sum(c.minsize for c in HISTORY_COLUMNS) + 2 * PAD + 40

    # -- layout --------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=(PAD, PAD, PAD, PAD))
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        self.head = ttk.Frame(outer)
        self.head.grid(row=0, column=0, sticky="ew")
        self._spread(self.head)

        body = ttk.Frame(outer)
        body.grid(row=1, column=0, sticky="nsew", pady=(2, 6))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(body, highlightthickness=0, borderwidth=0,
                               background=ROW_BG)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=bar.set)

        self.table = ttk.Frame(self.canvas)
        self.table_id = self.canvas.create_window((0, 0), window=self.table,
                                                  anchor="nw")
        self.table.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self.table_id, width=e.width))
        for widget in (self.canvas, self.table):
            widget.bind("<MouseWheel>", self._on_wheel)

        self.empty = ttk.Label(outer, text="", foreground="gray")
        self.empty.grid(row=2, column=0, sticky="w")

        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        # Sends one row's query back to the form on the left - hence "<<".
        self.btn_copy = ttk.Button(footer, text="<<", width=3,
                                   command=self.copy_to_main)
        self.btn_copy.pack(side="left", padx=(0, 6))
        self.btn_rerun = ttk.Button(footer, text="Rerun selected",
                                    command=self.rerun_selected)
        self.btn_rerun.pack(side="left")
        self.btn_delete = ttk.Button(footer, text="Delete selected",
                                     command=self.delete_selected)
        self.btn_delete.pack(side="left", padx=(6, 0))
        ttk.Button(footer, text="Refresh", command=self.reload).pack(
            side="left", padx=(6, 0))
        self.count_label = ttk.Label(footer, text="", foreground="gray")
        self.count_label.pack(side="right")

    def _spread(self, frame: ttk.Frame) -> None:
        """Identical column geometry for the header and the table body."""
        for i, col in enumerate(HISTORY_COLUMNS):
            frame.columnconfigure(i, minsize=col.minsize, weight=col.weight)
        # Trailing spacer soaks up whatever is left once columns are pinned,
        # so dragging one never has to fight the others for room.
        frame.columnconfigure(len(HISTORY_COLUMNS), weight=1, minsize=0)

    def _apply_widths(self) -> None:
        for frame in (self.head, self.table):
            self._spread(frame)

    # -- column resizing -----------------------------------------------

    def _begin_resize(self, event, index: int) -> None:
        # Start from the width actually on screen: a stretched column is
        # wider than its minsize, and jumping to minsize would look broken.
        self._drag = (index, event.x_root, self.head_cells[index].winfo_width())

    def _do_resize(self, event) -> None:
        index, x_start, width_start = self._drag
        col = HISTORY_COLUMNS[index]
        col.minsize = max(MIN_COL_WIDTH, width_start + event.x_root - x_start)
        # Dragging pins the column: it keeps the chosen width instead of
        # being restretched on the next resize.
        col.weight = 0
        self._apply_widths()

    def _end_resize(self, _event=None) -> None:
        r34dl.save_column_widths({c.key: c.minsize for c in HISTORY_COLUMNS})

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # -- data ----------------------------------------------------------

    def reload(self) -> None:
        keep = {rid for rid, var in self.checks.items() if var.get()}
        self.runs = r34dl.history_runs()
        self.checks = {}
        for run in self.runs:
            var = tk.BooleanVar(value=run["id"] in keep)
            var.trace_add("write", lambda *_: self._refresh_footer())
            self.checks[run["id"]] = var
        self._sort_runs()
        self._render()

    def _sort_runs(self) -> None:
        col = next(c for c in HISTORY_COLUMNS if c.key == self.sort_key)
        self.runs.sort(key=col.sort, reverse=self.sort_desc)

    def _on_heading(self, key: str) -> None:
        if key == "check":
            self._toggle_all()
            return
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            # Dates and counts are most useful biggest-first on first click.
            self.sort_desc = key in ("date", "matched", "downloaded", "ok")
        self._sort_runs()
        self._render()

    def _toggle_all(self) -> None:
        target = not all(v.get() for v in self.checks.values()) if self.checks else False
        for var in self.checks.values():
            var.set(target)
        self.header_all.set(target)

    # -- rendering -----------------------------------------------------

    def _render(self) -> None:
        for child in self.head.winfo_children():
            child.destroy()
        for child in self.table.winfo_children():
            child.destroy()

        self.head_cells = []
        for i, col in enumerate(HISTORY_COLUMNS):
            # Each heading is a cell holding the sort button plus a drag grip
            # on its right edge, so the grip always sits on the boundary.
            cell = ttk.Frame(self.head)
            cell.grid(row=0, column=i, sticky="ew")
            self.head_cells.append(cell)

            if col.key == "check":
                ttk.Checkbutton(cell, variable=self.header_all,
                                command=self._toggle_all).pack(side="left")
            else:
                arrow = ""
                if col.key == self.sort_key:
                    arrow = "  ▾" if self.sort_desc else "  ▴"
                # width=1 keeps the button from demanding more room than the
                # column allows - grid never shrinks a column below the widest
                # thing inside it.
                ttk.Button(cell, text=col.heading + arrow, width=1,
                           style="Head.TButton",
                           command=lambda k=col.key: self._on_heading(k)).pack(
                    side="left", fill="both", expand=True)

            grip = tk.Frame(cell, width=5, cursor="sb_h_double_arrow")
            grip.pack(side="right", fill="y")
            grip.bind("<Button-1>", lambda e, n=i: self._begin_resize(e, n))
            grip.bind("<B1-Motion>", self._do_resize)
            grip.bind("<ButtonRelease-1>", self._end_resize)

        self._spread(self.table)
        for r, run in enumerate(self.runs):
            self._render_row(r, run)

        self.empty.configure(
            text="" if self.runs else
            "No downloads recorded yet. Finished runs appear here.")
        self._refresh_footer()

    def _render_row(self, r: int, run: dict) -> None:
        bg = STRIPE_BG if r % 2 else ROW_BG
        cells = []
        for i, col in enumerate(HISTORY_COLUMNS):
            if col.key == "check":
                # tk rather than ttk so the row colour runs behind it too.
                w = tk.Checkbutton(self.table, variable=self.checks[run["id"]],
                                   bg=bg, activebackground=bg, bd=0,
                                   highlightthickness=0)
                w.grid(row=r, column=i, sticky="ew")
                cells.append(w)
                continue
            style = {}
            if col.key == "ok":
                style = {"fg": DONE_GREEN, "font": ("Segoe UI", 11, "bold")}
            # width=1 for the same reason as the headings: the cell must not
            # set a floor under the column the user is trying to narrow.
            label = tk.Label(self.table, text=col.render(run), anchor=col.anchor,
                             padx=4, bg=bg, width=1, **style)
            label.grid(row=r, column=i, sticky="ew")
            if col.key in EDITABLE_COLUMNS:
                label.configure(cursor="xterm")
                label.bind("<Double-Button-1>",
                           lambda _e, d=run, c=col, w=label: self._begin_edit(d, c, w))
            cells.append(label)

        for w in cells:
            w.bind("<MouseWheel>", self._on_wheel)

    def _refresh_footer(self) -> None:
        n = sum(1 for v in self.checks.values() if v.get())
        total = len(self.runs)
        self.count_label.configure(
            text=f"{n} of {total} selected" if total else "")
        state = "normal" if n else "disabled"
        self.btn_rerun.configure(state=state)
        self.btn_delete.configure(state=state)
        # Copying back only makes sense for exactly one row.
        self.btn_copy.configure(state="normal" if n == 1 else "disabled")

    # -- inline editing ------------------------------------------------

    def _begin_edit(self, run: dict, col: Column, label: tk.Label) -> None:
        """Double-click a tags or folder cell to edit it in place."""
        self._finish_edit(commit=True)          # only one editor at a time
        info = label.grid_info()
        var = tk.StringVar(value=str(run.get(col.key) or ""))
        entry = ttk.Entry(self.table, textvariable=var)
        entry.grid(row=info["row"], column=info["column"], sticky="ew")
        label.grid_remove()

        self._editor = {"entry": entry, "label": label, "var": var,
                        "run": run, "col": col, "closing": False,
                        "armed": False}
        # The click that opened this editor is still being dispatched, and it
        # will reach the application-wide click handler a moment from now.
        # Arm the click-away check only once that has passed, or the editor
        # would close itself the instant it opens.
        self.after_idle(self._arm_editor)
        entry.focus_set()
        entry.selection_range(0, "end")
        entry.icursor("end")
        entry.bind("<Return>", lambda _e: self._finish_edit(commit=True))
        entry.bind("<KP_Enter>", lambda _e: self._finish_edit(commit=True))
        entry.bind("<Escape>", lambda _e: self._finish_edit(commit=False))
        # Clicking elsewhere keeps the edit, matching how a spreadsheet behaves.
        entry.bind("<FocusOut>", lambda _e: self._finish_edit(commit=True))

    def _arm_editor(self) -> None:
        if self._editor:
            self._editor["armed"] = True

    def _click_outside(self, event) -> None:
        """Any click that isn't inside the open editor commits the edit."""
        state = self._editor
        if not state or not state["armed"]:
            return
        widget = event.widget
        while widget is not None:
            if widget is state["entry"]:
                return                      # clicked inside the editor itself
            widget = getattr(widget, "master", None)
        self._finish_edit(commit=True)

    def _finish_edit(self, commit: bool) -> None:
        state = self._editor
        # FocusOut fires again while we tear the entry down; ignore re-entry.
        if not state or state["closing"]:
            return
        state["closing"] = True
        self._editor = None

        run, col = state["run"], state["col"]
        new = state["var"].get().strip()
        old = str(run.get(col.key) or "")
        state["entry"].destroy()
        state["label"].grid()

        if not commit or new == old or not new:
            return
        if r34dl.update_history_run(run["id"], **{col.key: new}):
            run[col.key] = new
            self.app._append_log(
                f"[history] {col.heading.lower()} changed to {new!r}")
            self._render()
        else:
            messagebox.showerror("History", "Could not save that change.",
                                 parent=self.root)

    # -- actions -------------------------------------------------------

    def selected_runs(self) -> list[dict]:
        return [r for r in self.runs if self.checks[r["id"]].get()]

    def copy_to_main(self) -> None:
        """Push the single selected row's query back into the main form."""
        chosen = self.selected_runs()
        if len(chosen) != 1:
            return
        run = chosen[0]
        self.app.var_tags.set(str(run.get("tags") or ""))
        self.app.var_out.set(str(run.get("folder") or ""))
        self.app._append_log(
            f"[history] loaded {run.get('tags')!r} -> {run.get('folder')}")

    def delete_selected(self) -> None:
        chosen = self.selected_runs()
        if not chosen:
            return
        if not messagebox.askyesno(
                "Delete history",
                f"Remove {len(chosen)} entr{'y' if len(chosen) == 1 else 'ies'} "
                "from the history?\n\nDownloaded files are not touched.",
                parent=self.root):
            return
        removed = r34dl.delete_history_runs([r["id"] for r in chosen])
        self.app._append_log(f"[history] deleted {removed} entr"
                             f"{'y' if removed == 1 else 'ies'}")
        self.reload()

    def rerun_selected(self) -> None:
        """Re-check each selected query for new uploads, one after another."""
        chosen = self.selected_runs()
        if not chosen:
            return
        items = []
        for run in chosen:
            tags = str(run.get("tags") or "").strip()
            folder = str(run.get("folder") or "").strip()
            if tags and folder:
                items.append(self.app.options_for(tags, Path(folder)))
        if not items:
            messagebox.showerror("Rerun", "Those entries have no usable "
                                          "tags or folder.", parent=self.root)
            return
        self.app.launch(items, remember=False)


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=PAD)
        self.grid(row=0, column=0, sticky="nsew")
        # Column 1 is the history panel: fixed width, so a resize gives the
        # extra room to the controls rather than stretching the table.
        master.columnconfigure(0, weight=1)
        master.columnconfigure(1, weight=0)
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
        self.history: HistoryPanel | None = None
        self.history_open = False
        self.collapsed_width = 0
        self.base_min_width = 620
        self.base_min_height = 560
        self.queue_len = 1
        self.queue_pos = 0

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
        self.btn_history = ttk.Button(buttons, text="History  ▸",
                                      command=self.toggle_history)
        self.btn_history.pack(side="right")
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

    # Slide animation: short enough not to feel like waiting.
    SLIDE_STEPS = 9
    SLIDE_MS = 12

    def toggle_history(self) -> None:
        """Fold the history panel out of, or back into, the main window."""
        root = self.winfo_toplevel()
        if self.history is None:
            self.history = HistoryPanel(self, root)
        panel_w = HistoryPanel.panel_width()

        if self.history_open:
            start, end = root.winfo_width(), self.collapsed_width
            self.btn_history.configure(text="History  ▸")
        else:
            # Remember the folded width so closing restores it exactly.
            self.collapsed_width = root.winfo_width()
            start, end = self.collapsed_width, self.collapsed_width + panel_w
            self.history.reload()
            self.history.grid(row=0, column=1, sticky="nsew")
            self.btn_history.configure(text="History  ◂")

        self.history_open = not self.history_open
        # While the panel is out, spare width belongs to the table, not the
        # form: column 1 takes the stretch. This also makes the closing
        # animation shrink the panel rather than crushing the controls.
        root.columnconfigure(0, weight=0 if self.history_open else 1)
        root.columnconfigure(1, weight=1)
        # Let the window grow past the minimum before it is enforced.
        root.minsize(self.base_min_width + (panel_w if self.history_open else 0),
                     self.base_min_height)
        self._slide(root, start, end, 0)

    def _slide(self, root: tk.Misc, start: int, end: int, step: int) -> None:
        step += 1
        progress = step / self.SLIDE_STEPS
        eased = 1 - (1 - progress) ** 3          # ease-out
        width = int(start + (end - start) * eased)
        root.geometry(f"{width}x{root.winfo_height()}")
        if step < self.SLIDE_STEPS:
            root.after(self.SLIDE_MS,
                       lambda: self._slide(root, start, end, step))
        elif not self.history_open:
            # Only drop the panel out of the grid once it's fully hidden, and
            # hand the stretch back to the form.
            self.history.grid_remove()
            root.columnconfigure(1, weight=0)

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

    def options_for(self, tags: str, out_dir: Path) -> Options:
        """Build a run from the given query plus the window's current settings."""
        return Options(
            tags=tags,
            api_key=self.var_key.get().strip(),
            user_id=self.var_user.get().strip(),
            out_dir=out_dir,
            # 0 or blank means "download every match".
            limit=self._read_int(self.var_limit, "Max posts", minimum=0,
                                 allow_blank=True) or None,
            workers=self._read_int(self.var_workers, "Workers"),
            rate=self._read_int(self.var_rate, "Req/min"),
            verify=self.var_verify.get(),
            metadata=self.var_metadata.get(),
            dry_run=self.var_dryrun.get(),
        )

    def _start(self) -> None:
        try:
            opts = self.options_for(self.var_tags.get().strip(),
                                    Path(self.var_out.get().strip() or "downloads"))
            if not opts.tags:
                raise ValueError("Enter at least one tag")
        except ValueError as exc:
            messagebox.showerror("Check your settings", str(exc))
            return
        self.launch([opts])

    def launch(self, items: list[Options], remember: bool = True) -> None:
        """Run one or more queries back to back on the worker thread."""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running",
                                "Wait for the current download to finish, "
                                "or press Cancel.")
            return
        if not items:
            return
        missing = [o for o in items if not o.api_key or not o.user_id]
        if missing:
            messagebox.showerror("Check your settings",
                                 "Enter both your user ID and API key")
            return

        if self.var_remember.get():
            r34dl.save_credentials(items[0].api_key, items[0].user_id)
        if remember:
            # Only manual runs shape the folder list; a bulk re-check would
            # otherwise churn it with every folder it touches.
            self.combo_out.configure(
                values=r34dl.remember_folder(items[0].out_dir))

        self.cancel = threading.Event()
        self.queue_len = len(items)
        self.queue_pos = 0
        self._reset_run_state()
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.var_status.set("Searching...")
        if len(items) > 1:
            self._append_log(f"=== re-checking {len(items)} saved queries ===")

        self.worker = threading.Thread(target=self._work, args=(items,),
                                       daemon=True)
        self.worker.start()

    def _reset_run_state(self) -> None:
        self.total = None
        self.throttle_left = 0.0
        self.eta = None
        self.done = 0
        self.counts = {"saved": 0, "skipped": 0, "failed": 0}
        # Starts indeterminate; swaps to a real bar as soon as the API tells
        # us how many posts matched.
        self.progress.stop()
        self.progress.configure(value=0, maximum=100, mode="indeterminate")
        self.determinate = False
        self.progress.start(30)

    def _work(self, items: list[Options]) -> None:
        """Runs off the UI thread; everything goes back through the queue."""
        try:
            for index, opts in enumerate(items, start=1):
                if self.cancel.is_set():
                    break
                self.events.put(("item", (index, len(items), opts)))
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
                elif kind == "item":
                    self._on_item(*payload)
                elif kind == "total":
                    self._on_total(payload)
                elif kind == "throttle":
                    self._on_throttle(payload)
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_item(self, index: int, count: int, opts: Options) -> None:
        """A new query in the queue has started - reset the per-run readouts."""
        self.queue_pos = index
        self.queue_len = count
        self._reset_run_state()
        prefix = f"[{index}/{count}] " if count > 1 else ""
        self._append_log(f"--- {prefix}{opts.tags} -> {opts.out_dir} ---")
        self._refresh_status()

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
        queue = (f"query {self.queue_pos}/{self.queue_len}  |  "
                 if self.queue_len > 1 else "")
        text = (f"{queue}{self.done}{of_total} processed - "
                f"saved {self.counts['saved']}, "
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
        self.queue_len = 1
        # New rows will have landed in the DB - show them.
        if self.history is not None:
            self.history.reload()
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
    style = ttk.Style()
    try:
        # Slightly less dated-looking on Windows.
        style.theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    # Flat, left-aligned column headings for the history table.
    style.configure("Head.TButton", padding=(4, 2), relief="flat",
                    anchor="w", font=("Segoe UI", 9, "bold"))
    app = App(root)

    def on_close() -> None:
        app.cancel.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(launch())
