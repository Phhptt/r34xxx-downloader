# r34xxx-downloader

A desktop app for bulk-downloading posts from rule34.xxx by tag, using the
site's official API. Point it at a tag or an artist, pick a folder, and it
fetches everything that matches.

> **Heads up:** rule34.xxx is an adult site. You'll need your own account
> there to get an API key.

---

## What it does

- **Downloads a whole tag search** — every match, or a set number
- **Stays within the site's rate limit** automatically, so you don't get your
  key throttled or suspended
- **Shows real progress** — a proper progress bar and an estimated time to
  finish, rather than a spinner
- **Picks up where it left off** — anything already in the folder is skipped,
  so an interrupted download just resumes
- **Remembers your searches** — keep a list of artists and re-check them all
  for new uploads with one click
- **Works from the GUI or the command line**

---

## Installing

### Option 1 — download the app (Windows, no setup)

Grab `r34dl.exe` from the [Releases page][releases]. There's nothing to
install: put it in its own folder and run it. It creates its settings and
history files next to itself.

Windows may warn that the app is unrecognised, since the download isn't code
signed. Choose **More info → Run anyway** if you're happy to.

### Option 2 — run from source

Needs **Python 3.9 or newer**. On most systems Tkinter (for the window) comes
with Python already; on Debian/Ubuntu install it with
`sudo apt install python3-tk`.

```sh
git clone https://github.com/Phhptt/r34xxx-downloader.git
cd r34xxx-downloader
pip install -r requirements.txt
python r34gui.py
```

To build your own copy of the executable:

```sh
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name r34dl r34gui.py
```

---

## Setting up your API key

The site's API needs two things: an **API key** and your numeric **user ID**.
Both are on your account options page:

<https://rule34.xxx/index.php?page=account&s=options>

The simplest way to set them is to type them into the two fields at the top of
the window and tick **Remember credentials** — the app saves them and fills
them in next time.

If you'd rather not save them to disk, set the environment variables
`R34_API_KEY` and `R34_USER_ID` instead — the app checks those first.

Or copy `config.example.json` to `config.json` and fill it in by hand.

> The site's terms allow **one API key per user**. Don't request several.

---

## Using the app

Type your tags, choose where to save, and press **Download**.

**Tags** work exactly as they do on the website — separate them with spaces,
prefix with `-` to exclude, and the usual meta-tags apply:

```
artist_name                         everything by that artist
artist_name -animated               ...but skip videos
blue_eyes long_hair rating:safe     multiple tags plus a filter
some_tag sort:score:desc            highest scoring first
```

A few notes on the other fields:

| Field | What it does |
|---|---|
| **Save to** | Where files go. Remembers your recent folders in the dropdown. |
| **Max posts** | `0` means download everything that matches. Set a number to cap it. |
| **Workers** | How many files download at once. 4 is fine for most cases. |
| **Req/min** | Leave at 55. The site's limit is 60 per minute. |
| **Check MD5** | Warns if a file doesn't match the site's checksum. |
| **Save metadata** | Also writes each post's details to `metadata.jsonl`. |
| **Dry run** | Lists what *would* be downloaded without fetching anything. |

**Cancel** stops cleanly — no half-written files are left behind.

### The history panel

Press **History** to slide out a panel listing your past downloads. Each row
is one search: when it last ran, how many posts matched, and how many you
have.

This is the easy way to keep up with artists. Tick the ones you want, press
**Rerun selected**, and the app works through them in turn, downloading only
what's been posted since. Tick the box in the header to select everything.

- **Double-click** the tags or folder of a row to edit it
- **`<<`** loads a row's search back into the main form
- **Delete selected** removes rows from the list — your files are untouched

Re-running a search updates its existing row rather than adding a new one, so
the list stays a tidy set of things you follow.

---

## Command line

The same downloader without the window:

```sh
python r34dl.py "artist_name"                        # download everything
python r34dl.py "artist_name -animated" --out ./pics --workers 8
python r34dl.py "some_tag" --limit 50 --dry-run      # just look
```

| Flag | |
|---|---|
| `-o, --out` | where to save (default `./downloads`) |
| `-n, --limit` | maximum posts; `0` for all (default `0`) |
| `-w, --workers` | parallel downloads (default `4`) |
| `--rate` | requests per minute (default `55`) |
| `--verify` | check file checksums |
| `--metadata` | save post details to `metadata.jsonl` |
| `--dry-run` | list matches without downloading |

`python r34dl.py --help` lists the rest.

---

## Where things are saved

Files are named `20260816_18467236_54d16d4b….jpeg` — the post's upload date,
its ID, and its checksum — so folders sort neatly by date and nothing gets
downloaded twice.

Alongside the app you'll find `config.json` (your settings and key),
`history.db` (your saved searches) and `r34dl.log` (a small activity log).

> `config.json` holds your API key as plain text, so don't share that folder.

---

## A note on the code

This app is mostly **vibe-coded** — the bulk of it was written by an AI with a
human steering, reviewing and testing rather than typing. It works, it's used
daily, and it's been tested against the real API, but it hasn't had the kind
of careful review you'd want before trusting it with anything that matters.
Have a read before you run it, as you should with anything off the internet.

Bug reports and pull requests are welcome.

---

## Terms

Per the rule34.xxx API rules: one key per user, and no advertising or paywalls
over anything served from their CDN. They can disable any key at any time.

[releases]: https://github.com/Phhptt/r34xxx-downloader/releases
