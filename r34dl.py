#!/usr/bin/env python3
"""Download posts from rule34.xxx by tag search.

Run with no arguments to open the GUI; pass a tag query to use the CLI.

Credentials come from the environment (R34_API_KEY / R34_USER_ID), a
config.json next to this script, or --api-key / --user-id.

The site allows 60 requests per minute counting everything - API queries, file
downloads and retries alike - so all traffic goes through one shared rate
limiter regardless of how many worker threads are running.

Examples:
    python r34dl.py                                    # GUI
    python r34dl.py "blue_eyes long_hair" --limit 50
    python r34dl.py "artist_name -animated" --out ./pics --workers 8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests
from tqdm import tqdm

API_URL = "https://api.rule34.xxx/index.php"
PAGE_SIZE = 1000  # hard limit imposed by the API
USER_AGENT = "r34dl/1.0 (python-requests)"

# Site-wide budget is 60 requests/minute counting *everything* (API queries,
# file fetches, retries), so we stay a little under it by default.
DEFAULT_RATE = 55

# Under a PyInstaller --onefile build __file__ lives in a temp extraction dir,
# so anchor config next to the .exe instead.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "r34dl.log"
HISTORY_PATH = SCRIPT_DIR / "history.db"
LOG_MAX_BYTES = 1_000_000  # oldest lines are dropped past this


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

def fmt_duration(seconds: float | None) -> str:
    """Coarse, human-readable duration - false precision invites distrust."""
    if seconds is None:
        return "?"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours, rem = divmod(int(seconds), 3600)
    return f"{hours}h {rem // 60:02d}m"


# --------------------------------------------------------------------------
# debug log - capped, oldest lines dropped first
# --------------------------------------------------------------------------

class TrimmingLog:
    """Append-only log file that never exceeds `max_bytes`.

    When it would, the oldest whole lines are dropped from the front. Trimming
    cuts back to 90% so a full file doesn't rewrite itself on every line.
    """

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {message}\n".encode("utf-8")
        try:
            with self._lock:
                with open(self.path, "ab") as fh:
                    fh.write(line)
                if self.path.stat().st_size > self.max_bytes:
                    self._trim()
        except OSError:
            # Logging must never take a download down with it.
            pass

    def _trim(self) -> None:
        data = self.path.read_bytes()
        target = int(self.max_bytes * 0.9)
        if len(data) <= target:
            return
        # Cut at the first line boundary at or after the drop point, so we
        # never leave a half-line at the top of the file.
        cut = data.find(b"\n", len(data) - target)
        self.path.write_bytes(data[cut + 1:] if cut != -1 else b"")


LOG = TrimmingLog(LOG_PATH)


# --------------------------------------------------------------------------
# run history
# --------------------------------------------------------------------------

class History:
    """SQLite record of every run: what was asked for and how far it got.

    One short-lived connection per write - a run makes only a handful, and it
    keeps this safe to call from whichever worker thread gets there first.
    """

    def __init__(self, path: Path | None = None):
        # Resolved on construction, not at import: a default argument would
        # freeze the module-level path and make this untestable.
        self.path = Path(path or HISTORY_PATH)
        self.run_id: int | None = None
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        # Merging duplicates below deletes rows. Keep one copy of whatever the
        # database looked like beforehand, once, in case a merge is unwelcome.
        backup = self.path.with_suffix(".db.pre-merge.bak")
        if self.path.is_file() and not backup.exists():
            try:
                shutil.copy2(self.path, backup)
            except OSError as exc:
                LOG.write(f"HISTORY backup failed: {exc}")
        try:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        tags          TEXT    NOT NULL,
                        started_at    TEXT    NOT NULL,
                        finished_at   TEXT,
                        folder        TEXT    NOT NULL,
                        matched       INTEGER,
                        downloaded    INTEGER NOT NULL DEFAULT 0,
                        skipped       INTEGER NOT NULL DEFAULT 0,
                        failed        INTEGER NOT NULL DEFAULT 0,
                        duration_s    REAL,
                        status        TEXT    NOT NULL DEFAULT 'running'
                    )
                """)
                merge_duplicate_runs(conn)
        except sqlite3.Error as exc:
            LOG.write(f"HISTORY schema init failed: {exc}")

    def start(self, tags: str, folder: Path) -> None:
        """Claim the row for this query, reusing one if it already exists.

        The table is a compendium of saved queries rather than a log of runs:
        the same tags into the same folder is the *same* entry, re-executed.
        """
        tags = tags.strip()
        target = _normalise_folder(folder)
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with self._lock, self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM runs WHERE tags = ? "
                    "AND folder = ? COLLATE NOCASE ORDER BY id LIMIT 1",
                    (tags, target),
                ).fetchone()
                if existing:
                    self.run_id = existing[0]
                    conn.execute(
                        "UPDATE runs SET started_at = ?, finished_at = NULL, "
                        "status = 'running' WHERE id = ?", (now, self.run_id))
                else:
                    cur = conn.execute(
                        "INSERT INTO runs (tags, started_at, folder) "
                        "VALUES (?, ?, ?)", (tags, now, target))
                    self.run_id = cur.lastrowid
        except sqlite3.Error as exc:
            LOG.write(f"HISTORY start failed: {exc}")

    def set_matched(self, matched: int | None) -> None:
        self._update("matched = ?", (matched,))

    @staticmethod
    def _held(counts: dict) -> int:
        """How many of the matched posts are now on disk.

        Newly saved plus skipped: on a re-check the skips are the files an
        earlier run already fetched, so the pair reads as "you hold M of N"
        rather than "this run happened to download 3".
        """
        return counts["saved"] + counts["skipped"]

    def progress(self, counts: dict) -> None:
        """Checkpoint mid-run so a crash still leaves how far it got."""
        self._update("downloaded = ?, skipped = ?, failed = ?",
                     (self._held(counts), counts["skipped"], counts["failed"]))

    def finish(self, counts: dict, duration: float, status: str) -> None:
        self._update(
            "downloaded = ?, skipped = ?, failed = ?, duration_s = ?, "
            "finished_at = ?, status = ?",
            (self._held(counts), counts["skipped"], counts["failed"],
             round(duration, 1),
             datetime.now().isoformat(timespec="seconds"), status),
        )

    def _update(self, assignments: str, values: tuple) -> None:
        if self.run_id is None:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?",
                             (*values, self.run_id))
        except sqlite3.Error as exc:
            LOG.write(f"HISTORY update failed: {exc}")


class _NoHistory:
    """Stand-in used for dry runs, where nothing should be recorded."""

    run_id = None

    def start(self, *_a, **_k) -> None: ...
    def set_matched(self, *_a, **_k) -> None: ...
    def progress(self, *_a, **_k) -> None: ...
    def finish(self, *_a, **_k) -> None: ...


def merge_duplicate_runs(conn) -> int:
    """Collapse rows sharing tags and folder, keeping the most recent.

    Entries recorded before the table became a compendium can contain the same
    query several times over; so can renaming one entry onto another. Returns
    how many rows were removed.
    """
    rows = conn.execute(
        "SELECT id, tags, folder FROM runs "
        "ORDER BY COALESCE(finished_at, started_at) DESC, id DESC"
    ).fetchall()
    seen, doomed = set(), []
    for row in rows:
        key = (str(row[1]).strip(), os.path.normcase(str(row[2])))
        if key in seen:
            doomed.append(row[0])   # an older copy of a query we already kept
        else:
            seen.add(key)
    if not doomed:
        return 0
    conn.execute(f"DELETE FROM runs WHERE id IN ({','.join('?' * len(doomed))})",
                 doomed)
    LOG.write(f"HISTORY merged {len(doomed)} duplicate entr"
              f"{'y' if len(doomed) == 1 else 'ies'}")
    return len(doomed)


def history_runs(path: Path | None = None) -> list[dict]:
    """Every recorded run, newest first. Empty list if the DB isn't there yet."""
    db = Path(path or HISTORY_PATH)
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(db, timeout=10)
        conn.row_factory = sqlite3.Row
        with conn:
            rows = conn.execute(
                "SELECT id, tags, started_at, finished_at, folder, matched, "
                "downloaded, skipped, failed, duration_s, status "
                "FROM runs ORDER BY COALESCE(finished_at, started_at) DESC"
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        LOG.write(f"HISTORY read failed: {exc}")
        return []


def update_history_run(run_id: int, path: Path | None = None, **fields) -> bool:
    """Edit a recorded run in place. Only tags and folder may be changed."""
    allowed = {k: v for k, v in fields.items() if k in ("tags", "folder")}
    if not allowed:
        return False
    db = Path(path or HISTORY_PATH)
    if not db.is_file():
        return False
    try:
        with sqlite3.connect(db, timeout=10) as conn:
            assignments = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?",
                         (*allowed.values(), int(run_id)))
            # An edit can rename one entry onto another; entries are unique
            # per tags+folder, so fold them together.
            merge_duplicate_runs(conn)
        return True
    except sqlite3.Error as exc:
        LOG.write(f"HISTORY edit failed: {exc}")
        return False


def delete_history_runs(run_ids, path: Path | None = None) -> int:
    """Remove rows by id. Returns how many went."""
    ids = [int(i) for i in run_ids]
    if not ids:
        return 0
    db = Path(path or HISTORY_PATH)
    if not db.is_file():
        return 0
    try:
        with sqlite3.connect(db, timeout=10) as conn:
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", ids)
            return cur.rowcount
    except sqlite3.Error as exc:
        LOG.write(f"HISTORY delete failed: {exc}")
        return 0


@dataclass
class Options:
    """Everything a download run needs, shared by the CLI and the GUI."""
    tags: str
    api_key: str
    user_id: str
    out_dir: Path = Path("downloads")
    limit: int | None = None
    start_page: int = 0
    workers: int = 4
    rate: int = DEFAULT_RATE
    retries: int = 4
    verify: bool = False
    metadata: bool = False
    dry_run: bool = False


@dataclass
class Progress:
    """A single completed post, handed to the caller's progress callback."""
    status: str          # saved | skipped | failed
    detail: str
    counts: dict = field(default_factory=dict)
    eta: float | None = None   # seconds remaining, None while unknown


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter shared by every thread.

    Guarantees no more than `rate` acquisitions in any `period`-second window.
    """

    def __init__(self, rate: int, period: float = 60.0, on_wait=None):
        self.rate = max(1, rate)
        self.period = period
        # Called with the seconds left until the next slot while a thread is
        # blocked, then with 0.0 once it gets through, so a UI can show that
        # the program is throttling rather than hung.
        self.on_wait = on_wait
        self.used = 0  # total acquisitions, for batch accounting
        self._hits: list[float] = []
        self._lock = threading.Lock()
        self._last_notify = 0.0

    def free_slots(self) -> int:
        """Requests that could go out right now without waiting."""
        with self._lock:
            cutoff = time.monotonic() - self.period
            return max(0, self.rate - sum(1 for t in self._hits if t > cutoff))

    def seconds_until(self, requests: int) -> float:
        """Lower bound on the time to push `requests` more through the window.

        The window slides rather than resetting, so work done inside a period
        overlaps the wait instead of adding to it: `rate` requests clear every
        `period` seconds once the free slots are spent.
        """
        if requests <= 0:
            return 0.0
        queued = requests - self.free_slots()
        if queued <= 0:
            return 0.0
        return math.ceil(queued / self.rate) * self.period

    def _notify(self, seconds: float) -> None:
        if self.on_wait is None:
            return
        now = time.monotonic()
        # One update a second is plenty; several threads block at once.
        if seconds <= 0 or now - self._last_notify >= 1.0:
            self._last_notify = now
            self.on_wait(max(seconds, 0.0))

    def acquire(self, cancel: threading.Event | None = None) -> None:
        waited = False
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.period
                # Drop timestamps that have aged out of the window.
                self._hits = [t for t in self._hits if t > cutoff]
                got_slot = len(self._hits) < self.rate
                if got_slot:
                    self._hits.append(now)
                    self.used += 1
                    wait = 0.0
                else:
                    # Wait for the oldest hit to expire, then re-check: another
                    # thread may have claimed the slot that freed up.
                    wait = self._hits[0] - cutoff
            # Notify outside the lock - the callback may well ask the limiter
            # for an estimate, which would deadlock if we still held it.
            if got_slot:
                if waited:
                    self._notify(0.0)
                return
            waited = True
            self._notify(wait)
            # Sleep in short slices so cancellation stays responsive.
            time.sleep(min(max(wait, 0.01), 0.5))

    def penalise(self, seconds: float) -> None:
        """Burn the whole window after a 429 so every thread backs off together."""
        with self._lock:
            now = time.monotonic()
            self._hits = [now + seconds - self.period + 0.001] * self.rate


class Cancelled(Exception):
    """Raised internally when the caller asks a run to stop."""


# --------------------------------------------------------------------------
# time-to-complete estimation
# --------------------------------------------------------------------------

SAFETY_MARGIN = 1.05  # err slightly long: finishing early reads better


class Estimator:
    """Predicts how long the rest of a run will take.

    Two independent bounds, whichever is worse:

    * rate-bound - the limiter's own floor. Needs no measurement, so an
      estimate exists before the first file lands.
    * throughput-bound - measured completions per second. Dominates when the
      files are big enough that bandwidth, not the request cap, is the
      constraint.

    Skipped posts cost no request at all, so the observed skip rate is
    projected onto the remainder; without that, resuming a mostly-complete
    folder would over-predict several-fold.
    """

    def __init__(self, limiter: RateLimiter, total: int | None):
        self.limiter = limiter
        self.total = total
        self.started = time.monotonic()
        self.done = 0
        self.skipped = 0
        self._display: float | None = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def note(self, counts: dict) -> None:
        self.done = sum(counts.values())
        self.skipped = counts.get("skipped", 0)

    def remaining_requests(self) -> int:
        """Requests still to make.

        Derived from what the limiter has already spent rather than from
        completed posts: requests are consumed when a download *starts*, so
        counting finished posts ignores everything in flight and overstates
        the remainder by roughly one worker-load - which ceil() then rounds up
        into a whole extra window.
        """
        if self.total is None:
            return 0
        # Only trust the skip rate once there's a little evidence for it.
        if self.done >= 10:
            fetch_fraction = 1.0 - (self.skipped / self.done)
        else:
            fetch_fraction = 1.0
        pages = math.ceil(self.total / PAGE_SIZE)
        expected_total = math.ceil(self.total * fetch_fraction) + pages + 1
        return max(0, expected_total - self.limiter.used)

    def remaining_posts(self) -> int:
        if self.total is None:
            return 0
        return max(0, self.total - self.done)

    def raw_estimate(self) -> float | None:
        """Seconds remaining, unsmoothed, or None if not yet knowable."""
        if self.total is None:
            return None
        remaining = self.remaining_posts()
        if remaining <= 0:
            return 0.0

        rate_bound = self.limiter.seconds_until(self.remaining_requests())

        throughput_bound = 0.0
        if self.done > 0 and self.elapsed > 0:
            per_post = self.elapsed / self.done
            throughput_bound = remaining * per_post

        return max(rate_bound, throughput_bound) * SAFETY_MARGIN

    def estimate(self) -> float | None:
        """Smoothed seconds remaining.

        Falls to a lower value immediately but rises slowly: a jumpy ETA is
        the classic way these lose the reader's trust.
        """
        raw = self.raw_estimate()
        if raw is None:
            return None
        if self._display is None or raw < self._display:
            self._display = raw
        else:
            self._display += 0.2 * (raw - self._display)
        return self._display


# --------------------------------------------------------------------------
# config file - credentials and remembered folders
# --------------------------------------------------------------------------

MAX_RECENT_FOLDERS = 10


def read_config() -> dict:
    """The whole config as a dict; empty if missing or unreadable."""
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(**updates) -> None:
    """Merge `updates` into config.json, leaving other keys untouched."""
    cfg = read_config()
    cfg.update(updates)
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        LOG.write(f"CONFIG write failed: {exc}")


def load_credentials(cli_api_key: str | None = None,
                     cli_user_id: str | None = None) -> tuple[str, str]:
    """CLI args > environment > config.json. Returns ('', '') if unset."""
    api_key = cli_api_key or os.environ.get("R34_API_KEY")
    user_id = cli_user_id or os.environ.get("R34_USER_ID")

    if not api_key or not user_id:
        cfg = read_config()
        api_key = api_key or cfg.get("api_key") or ""
        user_id = user_id or str(cfg.get("user_id") or "")

    return api_key or "", str(user_id or "")


def save_credentials(api_key: str, user_id: str) -> None:
    """Persist credentials to config.json (used by the GUI's Remember box)."""
    write_config(api_key=api_key, user_id=user_id)


def _normalise_folder(folder: str | Path) -> str:
    """Absolute form of a folder path; tolerates one that doesn't exist yet."""
    try:
        return str(Path(folder).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(folder)


def recent_folders() -> list[str]:
    """Most-recently-used output folders, newest first."""
    folders = read_config().get("recent_folders")
    if not isinstance(folders, list):
        return []
    return [f for f in folders if isinstance(f, str) and f][:MAX_RECENT_FOLDERS]


def last_folder() -> str:
    """The folder used for the previous run, or '' if there wasn't one."""
    value = read_config().get("last_folder")
    return value if isinstance(value, str) else ""


def column_widths() -> dict:
    """Saved history-table column widths, keyed by column name."""
    widths = read_config().get("history_columns")
    if not isinstance(widths, dict):
        return {}
    return {k: int(v) for k, v in widths.items()
            if isinstance(v, (int, float)) and v > 0}


def save_column_widths(widths: dict) -> None:
    write_config(history_columns={k: int(v) for k, v in widths.items()})


def remember_folder(folder: str | Path) -> list[str]:
    """Record `folder` as most recently used and return the trimmed list.

    Deduplicated case-insensitively where the filesystem is (Windows), so the
    same directory typed with different capitalisation doesn't take two slots.
    """
    resolved = _normalise_folder(folder)
    key = os.path.normcase(resolved)
    kept = [f for f in recent_folders() if os.path.normcase(f) != key]
    folders = [resolved] + kept
    del folders[MAX_RECENT_FOLDERS:]
    write_config(last_folder=resolved, recent_folders=folders)
    return folders


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class Client:
    """Rate-limited rule34 API client with a pooled HTTP session."""

    def __init__(self, opts: Options, log=print, cancel: threading.Event | None = None,
                 on_throttle=None):
        self.opts = opts
        self.log = log
        self.cancel = cancel or threading.Event()
        self.limiter = RateLimiter(opts.rate, on_wait=on_throttle)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max(4, opts.workers),
            pool_maxsize=max(4, opts.workers),
            max_retries=0,  # retried here instead, so retries pass the limiter
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def close(self) -> None:
        self.session.close()

    # -- http ----------------------------------------------------------

    def get(self, url: str, *, params: dict | None = None, stream: bool = False,
            timeout: tuple[int, int] = (15, 60)) -> requests.Response:
        """GET with rate limiting and retry/backoff.

        Every attempt - including retries - takes a slot from the limiter,
        because the server counts them all against the same 60/minute budget.
        """
        last_exc: Exception | None = None
        for attempt in range(self.opts.retries + 1):
            self.limiter.acquire(self.cancel)
            try:
                resp = self.session.get(url, params=params, stream=stream,
                                        timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if resp.status_code == 429:
                    # We overshot: pause every thread, honouring Retry-After.
                    try:
                        cooldown = float(resp.headers.get("Retry-After") or 30)
                    except ValueError:
                        cooldown = 30.0
                    resp.close()
                    self.log(f"[rate] 429 received, pausing all requests for "
                             f"{cooldown:.0f}s")
                    self.limiter.penalise(min(cooldown, 300))
                    last_exc = requests.HTTPError("429 Too Many Requests")
                elif resp.status_code >= 500:
                    resp.close()
                    last_exc = requests.HTTPError(f"{resp.status_code} server error")
                else:
                    # Other 4xx won't fix themselves - fail loudly.
                    resp.raise_for_status()
                    return resp
            if attempt < self.opts.retries:
                self._sleep(min(2 ** attempt, 16) + random.uniform(0, 0.5))
        raise last_exc  # type: ignore[misc]

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep."""
        if self.cancel.wait(seconds):
            raise Cancelled()

    # -- api -----------------------------------------------------------

    def _params(self, tags: str, **extra) -> dict:
        return {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "tags": tags,
            "api_key": self.opts.api_key,
            "user_id": self.opts.user_id,
            **extra,
        }

    def count_posts(self, tags: str) -> int | None:
        """Total posts matching `tags`, or None if it can't be determined.

        Only the XML form of the response carries the total, as a `count`
        attribute on the root element - the JSON form drops it. Costs one
        request, which is worth it for a real progress bar.
        """
        resp = self.get(API_URL, params=self._params(tags, limit=1))
        try:
            root = ElementTree.fromstring(resp.text.strip())
        except ElementTree.ParseError:
            return None
        try:
            return int(root.attrib.get("count", ""))
        except ValueError:
            return None

    def fetch_page(self, tags: str, pid: int) -> list[dict]:
        """One page of posts.

        Uses the XML form rather than json=1: only XML exposes `created_at`
        (the real upload date, used for the filename prefix) and the total
        `count`. The JSON form has neither - its `change` field is a
        last-modified stamp that moves every time a post is retagged.
        """
        params = self._params(tags, limit=PAGE_SIZE, pid=pid)
        resp = self.get(API_URL, params=params)
        raw = resp.text.strip()
        if not raw:
            # The API returns an empty body once you page past the last result.
            return []
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError:
            snippet = re.sub(r"\s+", " ", raw)[:200]
            raise RuntimeError(f"unexpected response from API: {snippet}")
        return [normalise_post(el.attrib) for el in root.findall("post")]

    def iter_posts(self):
        """Yield post dicts, paging until exhausted, cancelled or limit hit."""
        yielded = 0
        pid = self.opts.start_page
        while not self.cancel.is_set():
            batch = self.fetch_page(self.opts.tags, pid)
            self.log(f"[api] page {pid}: {len(batch)} post(s)")
            if not batch:
                return
            for post in batch:
                if self.cancel.is_set():
                    return
                yield post
                yielded += 1
                if self.opts.limit is not None and yielded >= self.opts.limit:
                    return
            if len(batch) < PAGE_SIZE:
                return
            pid += 1

    # -- files ---------------------------------------------------------

    def download(self, post: dict, out_dir: Path,
                 existing_ids: set[str]) -> tuple[str, str]:
        """Returns (status, detail) where status is saved/skipped/failed."""
        post_id = str(post.get("id"))
        file_url = post.get("file_url")
        if not file_url:
            return "skipped", f"{post_id}: no file_url (deleted?)"
        if post_id in existing_ids:
            return "skipped", f"{post_id}: already on disk"

        dest = out_dir / target_name(post)
        if dest.exists():
            return "skipped", f"{dest.name}: already on disk"

        tmp = dest.with_suffix(dest.suffix + ".part")
        digest = hashlib.md5()
        written = 0
        try:
            with self.get(file_url, stream=True) as resp, open(tmp, "wb") as fh:
                declared = int(resp.headers.get("Content-Length") or 0)
                for chunk in resp.iter_content(64 * 1024):
                    if self.cancel.is_set():
                        raise Cancelled()
                    digest.update(chunk)
                    written += len(chunk)
                    fh.write(chunk)
            # A short read is the failure mode that actually matters: a dropped
            # connection leaves a plausible-looking but truncated file.
            if declared and written != declared:
                raise IOError(f"truncated: got {written} of {declared} bytes")
        except Cancelled:
            tmp.unlink(missing_ok=True)
            return "skipped", f"{post_id}: cancelled"
        except Exception as exc:  # network, disk, HTTP - reported the same way
            tmp.unlink(missing_ok=True)
            return "failed", f"{post_id}: {type(exc).__name__}: {exc}"

        # The API's `hash` is the md5 of the *original* upload. The CDN
        # re-encodes video (api-cdn-mp4) and recompresses some images, so a
        # mismatch is common and does not mean the download is damaged - warn,
        # but keep the file.
        expected = str(post.get("hash") or "").lower()
        if self.opts.verify and expected and digest.hexdigest() != expected:
            self.log(f"[warn] {post_id}: md5 differs from the API's original "
                     f"(CDN re-encode?) - keeping the file")

        tmp.replace(dest)
        return "saved", f"{dest.name} ({dest.stat().st_size / 1024:.0f} KiB)"


def normalise_post(attrib) -> dict:
    """Turn XML post attributes into the dict shape the rest of the code uses."""
    post = dict(attrib)
    # XML calls it md5; keep `hash` as the canonical key.
    post["hash"] = post.get("md5") or post.get("hash") or ""
    return post


# e.g. "Thu Jun 18 03:34:45 +0200 2026"
CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"


def post_date(post: dict) -> str:
    """The post's upload date as yyyymmdd, or '00000000' if unknown."""
    created = str(post.get("created_at") or "").strip()
    if created:
        try:
            return datetime.strptime(created, CREATED_AT_FORMAT).strftime("%Y%m%d")
        except ValueError:
            pass
    # Fall back to the last-change stamp - not the upload date, but closer
    # than nothing and still sorts sensibly.
    try:
        return datetime.fromtimestamp(int(post["change"]), tz=timezone.utc).strftime("%Y%m%d")
    except (KeyError, ValueError, TypeError, OSError):
        return "00000000"


def safe_ext(file_url: str) -> str:
    ext = Path(urllib.parse.urlparse(file_url).path).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".bin"


def target_name(post: dict) -> str:
    """'<yyyymmdd>_<id>_<md5>.<ext>'."""
    post_id = post.get("id")
    md5 = str(post.get("hash") or "")[:32]
    ext = safe_ext(post.get("file_url", ""))
    stem = f"{post_date(post)}_{post_id}"
    return f"{stem}_{md5}{ext}" if md5 else f"{stem}{ext}"


# Current '<date>_<id>_<md5>' plus the older '<id>_<md5>' layout, so files
# downloaded before the rename still count as already-present.
_NAME_PATTERNS = (
    re.compile(r"^\d{8}_(\d+)_[0-9a-f]{32}\."),
    re.compile(r"^(\d+)_[0-9a-f]{32}\."),
    re.compile(r"^(\d+)[._]"),
)


def scan_existing(out_dir: Path) -> set[str]:
    """Post ids already present, read back out of the filenames."""
    ids = set()
    for entry in out_dir.iterdir():
        if not entry.is_file():
            continue
        for pattern in _NAME_PATTERNS:
            if m := pattern.match(entry.name):
                ids.add(m.group(1))
                break
    return ids


# --------------------------------------------------------------------------
# orchestration - used by both front-ends
# --------------------------------------------------------------------------

def _log_batch_boundary(limiter: RateLimiter, opts: Options, state: dict,
                        history: "History", counts: dict, started: float,
                        eta: float | None) -> None:
    """Write a log checkpoint each time another window's worth of requests is spent.

    A "batch" here is `rate` requests - the amount that fits in one limiter
    window - which is the natural unit at which the prediction gets revised.
    """
    index = limiter.used // max(1, opts.rate)
    if index <= state["index"]:
        return

    now = time.monotonic()
    batch_time = now - state["at"]
    elapsed = now - started
    predicted_total = elapsed + eta if eta is not None else None
    old = state["last_prediction"]

    if state["index"] == 0:
        LOG.write(f"BATCH {index} (first): took {batch_time:.1f}s, "
                  f"predicted total {fmt_duration(predicted_total)}")
        state["first_prediction"] = predicted_total
    else:
        LOG.write(
            f"BATCH {index}: elapsed {elapsed:.0f}s, "
            f"old prediction {fmt_duration(old)}, batch took {batch_time:.1f}s, "
            f"new prediction {fmt_duration(predicted_total)}"
        )

    state["last_prediction"] = predicted_total
    state["index"] = index
    state["at"] = now
    # Checkpoint progress so a hard kill still leaves how far the run got.
    history.progress(counts)


def run_download(opts: Options, log=print, on_progress=None, on_total=None,
                 on_throttle=None, cancel: threading.Event | None = None) -> dict:
    """Run a full search-and-download pass. Returns the saved/skipped/failed counts.

    Callbacks, all invoked from worker threads (a GUI must marshal them back
    onto its event loop):
      log(str)              - human-readable progress lines
      on_progress(Progress) - one finished post
      on_total(int|None)    - how many posts this run will process, known up
                              front from the API's total count
      on_throttle(float)    - seconds until the rate limiter lets the next
                              request through; 0.0 once it does
    """
    if not opts.tags.strip():
        raise ValueError("no tags given")
    if not opts.api_key or not opts.user_id:
        raise ValueError("missing API key or user id")
    if opts.workers < 1:
        raise ValueError("workers must be at least 1")
    if opts.rate > 60:
        log(f"[warn] rate {opts.rate}/min exceeds the site's 60/min cap; "
            "expect 429s and a possible key suspension")

    out_dir = Path(opts.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    existing_ids = scan_existing(out_dir)
    if existing_ids:
        log(f"[init] {len(existing_ids)} post(s) already in {out_dir}")

    counts = {"saved": 0, "skipped": 0, "failed": 0}
    client = Client(opts, log=log, cancel=cancel, on_throttle=on_throttle)
    meta_fh = open(out_dir / "metadata.jsonl", "a", encoding="utf-8") if opts.metadata else None
    started = time.monotonic()

    # A dry run is a preview; it must not claim or alter a compendium entry.
    history = History() if not opts.dry_run else _NoHistory()
    history.start(opts.tags, out_dir)
    estimator = Estimator(client.limiter, None)
    # Batch = one window's worth of requests; used only for log checkpoints.
    batch_state = {"index": 0, "at": started, "first_prediction": None,
                   "last_prediction": None}
    status = "completed"

    def record(status_: str, detail: str) -> None:
        counts[status_] += 1
        estimator.note(counts)
        eta = estimator.estimate()
        if on_progress:
            on_progress(Progress(status_, detail, dict(counts), eta))
        _log_batch_boundary(client.limiter, opts, batch_state, history,
                            counts, started, eta)

    try:
        # One cheap request buys an exact denominator for the progress bar.
        matched = client.count_posts(opts.tags)
        if matched is not None:
            total = min(matched, opts.limit) if opts.limit else matched
            log(f"[api] {matched} post(s) match; downloading {total}")
            if matched == 0:
                if on_total:
                    on_total(0)
                LOG.write(f"RUN START tags={opts.tags!r} matched=0 "
                          f"folder={out_dir.resolve()} - nothing to do")
                return counts
        else:
            total = opts.limit
            log("[api] total count unavailable; progress will be approximate")

        history.set_matched(matched)
        estimator.total = total

        # The rate-bound estimate needs no measurement, so there is already a
        # prediction to log before the first file lands.
        expected_requests = (total or 0) + math.ceil((total or 0) / PAGE_SIZE)
        batches = math.ceil(expected_requests / max(1, opts.rate))
        upfront = client.limiter.seconds_until(expected_requests) * SAFETY_MARGIN
        batch_state["upfront_prediction"] = upfront
        LOG.write(
            f"RUN START tags={opts.tags!r} matched={matched} to_download={total} "
            f"batches={batches} rate={opts.rate}/{60}s workers={opts.workers} "
            f"folder={out_dir.resolve()} upfront_prediction={fmt_duration(upfront)}"
        )
        if total:
            log(f"[eta] estimated {fmt_duration(upfront)} "
                f"({batches} batch(es) at {opts.rate} req/min)")
        if on_total:
            on_total(total)

        if opts.dry_run:
            for post in client.iter_posts():
                if meta_fh:
                    meta_fh.write(json.dumps(post, ensure_ascii=False) + "\n")
                detail = f"{target_name(post)} <- {post.get('file_url')}"
                log(f"[dry-run] {detail}")
                record("skipped", detail)
        else:
            with ThreadPoolExecutor(max_workers=opts.workers) as pool:
                pending: set = set()
                try:
                    for post in client.iter_posts():
                        if meta_fh:
                            meta_fh.write(json.dumps(post, ensure_ascii=False) + "\n")
                        pending.add(pool.submit(client.download, post, out_dir,
                                                existing_ids))
                        # Bound the queue so a huge search doesn't buffer in RAM.
                        if len(pending) >= opts.workers * 8:
                            done = next(as_completed(pending))
                            pending.discard(done)
                            record(*done.result())
                finally:
                    for fut in as_completed(pending):
                        record(*fut.result())
    except Cancelled:
        status = "cancelled"
        log("[abort] cancelled")
    except BaseException as exc:
        status = "error"
        LOG.write(f"RUN ERROR {type(exc).__name__}: {exc}")
        raise
    finally:
        client.close()
        if meta_fh:
            meta_fh.close()
        actual = time.monotonic() - started
        # Cancellation is mostly cooperative - iter_posts just stops and
        # download() reports "cancelled" - so no exception need reach here.
        # Ask the flag rather than inferring from control flow.
        if status == "completed" and client.cancel.is_set():
            status = "cancelled"
        history.finish(counts, actual, status)
        _log_run_end(batch_state, counts, actual, status)

    log(f"Done in {actual:.1f}s - saved {counts['saved']}, "
        f"skipped {counts['skipped']}, failed {counts['failed']}. "
        f"Output: {out_dir.resolve()}")
    return counts


def _log_run_end(state: dict, counts: dict, actual: float, status: str) -> None:
    """Final line: how the prediction actually held up."""
    # Prefer the post-first-batch prediction - the up-front one is a floor
    # computed before anything was measured.
    predicted = state.get("first_prediction") or state.get("upfront_prediction")
    parts = [
        f"RUN END status={status}",
        f"actual={actual:.1f}s ({fmt_duration(actual)})",
        f"predicted={fmt_duration(predicted)}",
    ]
    if predicted and predicted > 0:
        drift = (actual - predicted) / predicted * 100
        parts.append(f"decorrelation={drift:+.1f}%")
    else:
        parts.append("decorrelation=n/a")
    parts.append(f"saved={counts['saved']} skipped={counts['skipped']} "
                 f"failed={counts['failed']}")
    LOG.write(" ".join(parts))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download rule34.xxx posts by tag. Run with no arguments "
                    "to open the GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Tag syntax is the same as the website: space-separated, '-tag' to\n"
            "exclude, and meta-tags such as sort:score:desc, rating:safe,\n"
            "score:>100, id:>1000000 all work.\n"
        ),
    )
    p.add_argument("tags", nargs="?", help="tag query, e.g. \"tag_a tag_b -tag_c\"")
    p.add_argument("--gui", action="store_true", help="force the GUI to open")
    p.add_argument("-o", "--out", default="downloads", type=Path,
                   help="output directory (default: ./downloads)")
    p.add_argument("-n", "--limit", type=int, default=0,
                   help="max posts to process; 0 means every match (default: 0)")
    p.add_argument("-p", "--start-page", type=int, default=0,
                   help="first API page to fetch (default: 0)")
    p.add_argument("-w", "--workers", type=int, default=4,
                   help="parallel downloads (default: 4)")
    p.add_argument("--rate", type=int, default=DEFAULT_RATE,
                   help=f"max requests per minute, site cap is 60 "
                        f"(default: {DEFAULT_RATE})")
    p.add_argument("--retries", type=int, default=4,
                   help="retries per failed request (default: 4)")
    p.add_argument("--verify", action="store_true",
                   help="compare each file's md5 against the API's and warn on a "
                        "mismatch (common for CDN-transcoded video; truncated "
                        "downloads are always rejected regardless)")
    p.add_argument("--metadata", action="store_true",
                   help="append each post's JSON to metadata.jsonl in the output dir")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be downloaded without fetching files")
    p.add_argument("--api-key", help="override the configured API key")
    p.add_argument("--user-id", help="override the configured user id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.gui or not args.tags:
        from r34gui import launch
        return launch()

    if args.limit < 0:
        sys.exit("error: --limit cannot be negative (use 0 for every match)")
    limit = args.limit or None

    api_key, user_id = load_credentials(args.api_key, args.user_id)
    if not api_key or not user_id:
        sys.exit(
            "error: missing credentials.\n"
            f"  Put them in {CONFIG_PATH} as {{\"api_key\": \"...\", \"user_id\": \"...\"}},\n"
            "  or set R34_API_KEY / R34_USER_ID, or pass --api-key / --user-id.\n"
            "  Get a key at https://rule34.xxx/index.php?page=account&s=options"
        )

    opts = Options(
        tags=args.tags, api_key=api_key, user_id=user_id, out_dir=args.out,
        limit=limit, start_page=args.start_page, workers=args.workers,
        rate=args.rate, retries=args.retries, verify=args.verify,
        metadata=args.metadata, dry_run=args.dry_run,
    )

    # tqdm's own {remaining} is a pure throughput extrapolation, which reads
    # wildly optimistic right up to a throttle pause and wildly pessimistic
    # during one. Drop it and show the estimator's figure instead.
    bar = tqdm(total=limit, unit="post", desc="downloading", disable=args.dry_run,
               bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                          "[{elapsed}]{postfix}")
    cancel = threading.Event()

    def on_progress(p: Progress) -> None:
        bar.update(1)
        bar.set_postfix_str(
            f"saved={p.counts['saved']} skipped={p.counts['skipped']} "
            f"failed={p.counts['failed']} eta={fmt_duration(p.eta)}",
            refresh=False)
        if p.status == "failed":
            tqdm.write(f"[failed] {p.detail}")

    def on_total(total: int | None) -> None:
        bar.reset(total=total)

    def on_throttle(seconds: float) -> None:
        # Without this the bar just sits there during a rate-limit pause and
        # looks hung.
        # The ETA already accounts for this wait (it is the rate-bound term),
        # so only the description changes here.
        bar.set_description(f"rate limit, {seconds:.0f}s" if seconds > 0
                            else "downloading")

    try:
        counts = run_download(opts, log=tqdm.write, on_progress=on_progress,
                              on_total=on_total, on_throttle=on_throttle,
                              cancel=cancel)
    except KeyboardInterrupt:
        cancel.set()
        bar.close()
        print("\n[abort] interrupted by user")
        return 130
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        bar.close()
        print(f"[error] {exc}")
        return 1
    finally:
        bar.close()

    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
