# rule34.xxx downloader

Bulk-downloads posts from rule34.xxx by tag search, through the site's
official API. Ships a small desktop GUI and an equivalent command-line
interface.

*(rule34.xxx hosts adult content. You need your own account and API key.)*

- Downloads every match for a tag query, or a capped number
- Respects the site's 60 requests/minute limit with one shared limiter, so
  more workers means more concurrency, not more requests
- Real progress bar and time-remaining estimate, including during throttle
  pauses
- Resumes for free — anything already in the output folder is skipped
- Per-run logging and a SQLite history of every run

---

## Requirements

- **Python 3.9+** (developed and tested on 3.12)
- **Tkinter**, for the GUI. Bundled with Python on Windows and macOS; on
  Debian/Ubuntu install it with `sudo apt install python3-tk`
- A rule34.xxx account, for the API key

## Installation

```sh
git clone https://github.com/<your-username>/rule34xxx-downloader.git
cd rule34xxx-downloader
pip install -r requirements.txt
```

That installs `requests` and `tqdm`. `pyinstaller` is only needed if you want
to build a standalone `.exe` (see the bottom of this file).

## API credentials

Get a key from your account options page:
<https://rule34.xxx/index.php?page=account&s=options>

You need both the **API key** and your numeric **user ID**. Supply them in
any one of three ways — they're checked in this order:

1. **Command-line flags** — `--api-key ... --user-id ...`
2. **Environment variables** — `R34_API_KEY` and `R34_USER_ID`
3. **`config.json`**, next to the script:

```sh
cp config.example.json config.json    # then edit in your key and user ID
```

In the GUI you can simply type them into the fields and tick **Remember
credentials**, which writes `config.json` for you.

`config.json` is gitignored — don't commit it, and don't paste your key
anywhere public. The site's terms allow **one key per user**; requesting or
using several risks suspension.

The GUI also keeps `last_folder` and `recent_folders` in the same file. Those
are written automatically; you never need to add them by hand.

## Usage — GUI

```sh
python r34gui.py        # or: python r34dl.py   (no arguments opens the GUI)
```

Fill in the tags and press **Download**. Notes on the fields:

- **Save to** is an editable dropdown. It opens on the folder you last
  downloaded to and remembers the last 10, newest first — pick one from the
  list, type a path, or use **Browse…**. A folder is only remembered once a
  run actually starts, so browsing around without downloading doesn't clutter
  it.
- **Max posts** defaults to `0`, meaning *download every match*. Set a number
  only to cap a run.
- **Req/min** defaults to 55, just under the site's cap of 60.
- **Cancel** stops cleanly; a partly-written file is deleted, never left
  behind looking complete.
- Every text field supports **Ctrl+Z** / **Ctrl+Y** (or Ctrl+Shift+Z) for
  undo and redo, with independent history per field.

## Usage — CLI

```sh
python r34dl.py "karoshizoe blonde_hair"                        # every match
python r34dl.py "artist_name -animated" --out ./pics --workers 8
python r34dl.py "some_tag sort:score:desc" --limit 50 --metadata --dry-run
```

| Flag | Meaning |
|---|---|
| `-o, --out` | output directory (default `./downloads`) |
| `-n, --limit` | max posts; `0` means every match (default `0`) |
| `-p, --start-page` | first API page, 1000 posts each (default `0`) |
| `-w, --workers` | parallel downloads (default `4`) |
| `--rate` | requests per minute (default `55`, site cap `60`) |
| `--retries` | retries per failed request (default `4`) |
| `--verify` | compare md5s and warn on mismatch |
| `--metadata` | append each post's fields to `metadata.jsonl` |
| `--dry-run` | list what would be fetched, download nothing |
| `--api-key`, `--user-id` | override the configured credentials |

Tag syntax is exactly the website's: space-separated, `-tag` excludes, and
meta-tags such as `sort:score:desc`, `rating:safe`, `score:>100` and
`id:>1000000` all work.

## Output files

Files are named `<yyyymmdd>_<post_id>_<md5>.<ext>`, where the date is the
post's upload date, so a folder sorts chronologically.

- **Resuming is automatic.** Re-running a search skips anything already in
  the output folder. Files saved under the older `<post_id>_<md5>.<ext>`
  scheme are still recognised and won't be fetched twice.
- **Partial files can't masquerade as complete.** Downloads land in a `.part`
  file and are renamed only once finished, and a response shorter than its
  `Content-Length` is rejected outright.
- **`--verify` only warns.** The API's `hash` is the md5 of the *original*
  upload, but the CDN re-encodes video (`api-cdn-mp4`) and recompresses some
  images, so a mismatch is normal and the file is still good.

## Rate limiting

The site allows **60 requests per minute counting everything** — searches,
file downloads, and retries alike. All traffic passes through a single
sliding-window limiter shared by every worker thread, so raising `--workers`
increases concurrency without increasing the request rate. The default of 55
leaves headroom. On a `429` the limiter parks every thread for the
`Retry-After` interval instead of hammering on.

## Progress and time remaining

One extra request up front asks the API how many posts match, so the progress
bar has a real denominator (`37 of 412 processed`) rather than a guess.
During a throttle pause both front-ends show a live countdown — `rate limit:
next request in 47s` — so a waiting run reads as working rather than hung.

The `~14 min left` figure is the larger of two independent bounds:

- **rate-bound** — the limiter's own floor, `ceil(remaining / rate) × period`.
  Needs no measurement, so an estimate exists before the first file lands.
  The window *slides*, so work done inside a period overlaps the wait instead
  of adding to it.
- **throughput-bound** — measured completions per second. Takes over when
  files are large enough that bandwidth, not the request cap, is the limit.

Skipped posts cost no request, so the observed skip rate is projected onto
the remainder; without that, resuming a mostly-complete folder would
over-predict several-fold. Remaining work is counted from requests the
limiter has actually spent rather than from finished posts, so downloads
still in flight are accounted for. The estimate carries a 5% margin (it errs
long), drops to reality instantly, and rises only gradually. In practice it
lands within about 5% on long runs.

## Logs and history

`r34dl.log` — written beside the script (or beside the `.exe`) — records each
run: the query and target folder, the batch count, how long each batch took,
how the prediction moved, and a closing line comparing predicted against
actual. It is capped at **1 MB**; past that the oldest whole lines are
dropped from the front.

```
RUN START tags='example_tag' matched=1730 to_download=1730 batches=32 ...
BATCH 1 (first): took 2.8s, predicted total 33 min
BATCH 2: elapsed 63s, old prediction 33 min, batch took 60.5s, new prediction 33 min
RUN END status=completed actual=1863.2s (31 min) predicted=33 min decorrelation=-4.7% ...
```

`history.db` is a SQLite record of every run — tags, timestamps, folder,
posts matched, downloaded, skipped, failed, duration, and status
(`completed` / `cancelled` / `error`). Progress is checkpointed each batch, so
even a run that is killed outright leaves a row showing how far it got.
Nothing reads it back yet; it exists for a history view later.

```sh
sqlite3 history.db "SELECT started_at, tags, downloaded, matched, status FROM runs"
```

## Building a standalone .exe

Produces a single ~15 MB Windows binary that needs no Python installation.
The built artifact is **not** included in this repository.

```sh
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name r34dl --noconfirm --clean r34gui.py
copy config.json dist\config.json
```

The exe resolves `config.json`, `r34dl.log` and `history.db` **beside
itself**, not in the working directory — hence copying the config into
`dist/`. Alternatively leave the fields blank on first run, type your
credentials in, and tick *Remember credentials*; the environment variables
work too.

`dist/config.json` stores the key in plaintext, so don't hand that folder to
anyone as-is. The exe's default output folder is `downloads/` next to the
binary.

## Implementation notes

- Post listings are read from the API's **XML** form rather than `json=1`.
  Only the XML response exposes `created_at` (the true upload date used in
  the filename) and the total match count. `--metadata` therefore records the
  XML field set, which includes each post's tags.
- Cloudflare fronts both the API and the CDN but does not currently challenge
  this traffic or police the user-agent. If a run ever fails with a bare
  `403` where your key previously worked, suspect a challenge rather than
  your credentials, and slow `--rate` down.

## API terms

From the site's API documentation: don't display advertisements or run
paywalls over content served from their CDN, and use only one API key per
user. They reserve the right to disable any key.
