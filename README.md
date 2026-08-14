# Lidarr Accessory (`cue_pipeline`)

A companion service for **Lidarr** that handles what Lidarr itself won't: disc
images, exotic formats, discography torrents, albums whose folder name matches
nothing in any script, and gaps that sit missing forever. It runs as a container
beside Lidarr (or as a Windows service) and has its own web UI on port **8830**.

Lidarr decides *what* belongs in the library. This decides *how to get it in
there* when the normal path fails, and it is deliberately conservative: when it
cannot tell two things apart it downloads rather than deletes, and keeps rather
than discards.

---

## Contents

- [What it actually does](#what-it-actually-does)
- [Web UI](#web-ui)
- [The passes](#the-passes)
- [How an album is identified](#how-an-album-is-identified)
- [The converter](#the-converter)
- [Safety rules](#safety-rules)
- [Install](#install)
- [Configuration](#configuration)
- [Deploying a code change](#deploying-a-code-change)
- [State files](#state-files)
- [HTTP API](#http-api)
- [Troubleshooting](#troubleshooting)

---

## What it actually does

**Imports what Lidarr can't.** Splits `.cue` disc images with ffmpeg, decodes
DTS-CD and DSD, extracts SACD and DVD-Audio ISOs, unpacks archives, transcodes
APE/WavPack/ALAC/WAV to FLAC, and — when the folder name and the tags are both
useless — identifies an album from its **song titles**.

**Finds music Lidarr left missing.** Searches per album first; only when an
album can't be found alone does it fall back to the artist scope and consider a
discography. Candidates are verified inside qBittorrent before they count.

**Keeps discography torrents lean.** Deselects albums you already own — whole
folders, not just the audio — so a 40 GB discography downloads only the gaps.

**Registers music that's already on disk.** A library folder can hold files
Lidarr has no record of; the audit finds and imports them by explicit track id.

**Assembles albums that exist only in pieces.** Works out which songs of a
missing album sit inside compilations you already have.

**Hands you the rest.** Anything it won't decide alone lands in **Needs
attention** with the evidence.

An LLM (Ollama, or any OpenAI-compatible endpoint) is optional and now used in
only three places — cue repair, artist/album parsing, and confirming an album
match. Everything else is deterministic. With the LLM off, every one of those
paths still works.

---

## Web UI

Port **8830**.

| Tab | What it's for |
|---|---|
| **Needs attention** | Albums needing a decision, with a track-level comparison against the library. Add to library / Overwrite / Discard, per row or in bulk. |
| **Assembly** | Missing albums buildable from songs you already own: percent assembled, which songs are still missing, and which are already in the library. |
| **Converter** | Library browser + transcoder, with live progress. |
| **Log** | Tail of `pipeline.log`, with a **Clear** button that also removes the rotated copies. |
| **Settings** | Every tunable below, grouped, saved to `webui_overrides.json`. |

The Converter's controls: **Convert**, **Clear** (empties the queue; anything
already encoding finishes), **Pause/Resume**. Progress is shown as one total bar
at the top plus a bar per section, and the queue is grouped **by album** — a
whole-library run is ~86,000 files and no flat list of those is readable.

---

## The passes

Each runs on its own schedule; all are individually switchable.

| Pass | Job |
|---|---|
| **Cue watcher + worker** | Watches `/downloads` for `.cue`, splits disc images, hands the result to Lidarr. |
| **Cueless sweep** | Folders with no `.cue`: pre-split albums, ISOs, archives, DSD/DTS. |
| **Library audit** | Walks the library itself for albums Lidarr has wrong, missing or **under-registered**. |
| **Reconcile** | Asks Lidarr for its own missing list and re-probes `/downloads` for anything that fits. |
| **Interactive search** | Grabs releases for monitored albums that are still missing. |
| **qBittorrent deselect** | Trims discography torrents down to what you don't own. |
| **Lifecycle / reapers** | Removes fully-imported torrents, dead grabs, stalled downloads. |
| **Song harvest** | Pulls individual missing songs out of compilations already on disk. |
| **Album assembly** | Builds a missing album from songs scattered across other releases. |
| **External audit** | Compares Lidarr against MusicBrainz for albums it has no record of. |

---

## How an album is identified

This is the part that took the most work, because names are the least reliable
thing in a music library. The chain, in order:

1. **Lidarr's own matcher** (`manualimport`). If it accepts with no rejections,
   done.
2. **Tag album + folder name**, normalised: accents folded, `&`→`and`, editions
   and year suffixes stripped.
3. **Cyrillic transliteration** — `Азис` == `Azis`, `Лили Иванова` ==
   `Lili Ivanova`.
4. **Mojibake repair** — Cyrillic written as cp1251 and read as latin-1
   (`Ëèëè Èâàíîâà` → `Лили Иванова`) is endemic in older rips. The test is
   strict, so `Beyoncé` and `Motörhead` are untouched.
5. **Romanisation variants** — `Dimash Kudaibergen` → `Dimash Qudaibergen`,
   accepted only at ≥0.90 similarity with a clear margin over the runner-up.
6. **MusicBrainz aliases** — the authoritative answer. `Lauryn Hill` →
   `Ms. Lauryn Hill`, and it covers stage names and misspellings. Only an exact
   normalised hit on a name, sort-name or alias is accepted; results are cached
   including misses, because MusicBrainz allows about one request a second.
7. **Song titles** — when no name test can work, compare the folder's songs to
   each incomplete album's track list. Requires ≥60% of the album's tracks
   present *and* a clear margin over the runner-up.

Some things that fall out of this:

- **Same-titled albums are told apart by year.** Weezer has seven albums called
  *Weezer*. If the year can't decide and any of them is incomplete, the answer
  is "not owned" — deselecting on a coin flip is how the one album you're
  missing stops downloading.
- **Non-Latin names get distinct keys.** Arabic and CJK names used to fold to
  the empty string and collide with each other.
- **Multi-disc albums stay one album.** `12 Vinyl 01`, `Enhanced CD 02`,
  `Hybrid SACD (SACD layer, 2 channels) 02` are *media*, not albums.
- **Files are mapped to tracks by three indexes** — `(medium, track)`, ordered
  position, then title — because a multi-disc release numbers tracks both ways
  and files follow either convention.

---

## The converter

A library browser that transcodes to MP3 / AAC / Opus / FLAC / WAV with
dbPoweramp-style per-codec settings.

| Option | Meaning |
|---|---|
| **Output to** | Beside the original, or any unassigned drive (free space shown). |
| **Root folder** | Folder under that drive everything goes into — picked from what's already there, or created with **+**. |
| **Skip already converted** | Don't re-encode when the output exists. Off, you get `(1)`, `(2)`, `(3)` copies on every run. |
| **Copy lossy instead of skipping** | With *lossless sources only*, carry already-lossy files across untouched so the destination holds the whole selection. |
| **Lossless sources only** | Skip sources that are already lossy. |
| **Delete original** | Replace the source after a **verified** encode. Ignored when writing to another drive. |
| **Bit depth** | `original` / 16 / 24 (FLAC), plus 32 for WAV. No 32-bit FLAC — the encoder silently writes 24. |

**It yields to the pipeline.** A manual conversion is the lowest-priority work
on the box: it holds its queue while a cue split or SACD/DVD-Audio extraction is
running, and its ffmpeg runs under `ionice -c 3 nice -n 15`. Override with
`CONVERT_NICE`.

**The queue survives a restart** (`convert_queue.json`). Anything mid-encode
comes back queued, never resumed — a half-written output was never committed.

---

## Safety rules

These are load-bearing. Most exist because something went wrong once.

- **Nothing is deleted without proof it's elsewhere.** A source folder is
  removed only after the import is verified in Lidarr.
- **A bigger source is never deleted.** If the disc image holds more tracks than
  Lidarr's edition, the source stays.
- **`replaceExistingFiles` is not available.** Combined with `importMode=move`
  on a file already inside the library, Lidarr moves the file onto itself and
  deletes the "existing" copy. With the recycle bin off that is permanent, and
  it destroyed an album. The capability is gone from the client entirely.
- **Prefer-lossless quarantines, it doesn't delete.** The redundant lossy twin
  is moved to `<album>/_superseded_by_lossless/` and Lidarr is asked to rescan.
- **A lossy file with no lossless twin is kept.**
- **Ambiguity favours downloading.** Two artists with the same album title, a
  title matching several albums, an unreadable folder — all resolve to "keep".
- **Mis-parses err toward KEEP**, never toward deselecting something you don't
  have.

### What it refuses to grab

- **Video releases** — BDRip, Blu-ray, HDTV, x264, 720p/1080p, mkv. DVD-Audio,
  Blu-ray Audio and SACD are exempt: those are music.
- **Non-audio payloads** — 3D model packs, ebooks, software.
- **Oversized releases** — more than `max_gb_per_album` (default 2.5 GB) per
  album it would actually fill. Stops a 27 GB discography being grabbed to fill
  one album.
- **Placeholder artists** — `Various Artists`, `VA`, `Soundtrack`, `Unknown`.
  Their names appear in most compilation titles, so an artist-scope search on
  one matches nearly anything.
- **Short artist names that only match mid-title** — `M.I.A.` must not match
  `A.R.M.I.A`.

---

## Install

### Docker (Unraid)

```bash
docker run -d --name cue_pipeline --user 99:100 \
  -p 8830:8830 \
  -v /mnt/cache/appdata/cue_pipeline:/config \
  -v "/path/to/downloads":/downloads \
  -v /mnt/user/Audio:/music \
  -v /mnt/cache/appdata/lidarr/logs:/lidarr-logs:ro \
  -v /mnt/disks:/unassigned \
  cue_pipeline:latest
```

Every mount must be non-empty at start. An empty `/downloads` reads as "every
torrent was already imported" — that once removed 85 torrents.

### Windows service

`install_service.bat` (uninstall with `uninstall_service.bat`). Needs Python
3.11+, ffmpeg/ffprobe on `PATH`, and `pip install -r requirements.txt`.

---

## Configuration

`config.yaml` is the base; the Settings tab writes `webui_overrides.json`, which
wins. Environment variables sit in between.

> **Adding a setting takes three edits** — the `OrchestratorConfig` field, the
> settings-registry tuple *and* its group entry, and the `OrchestratorConfig(...)`
> call in `main.py`. Miss the third and the UI shows your saved value while the
> pipeline runs the default, with no error.

Settings are grouped in the UI as: *Finding missing music*, *Which release to
accept*, *Importing*, *Torrents*, *Needs attention & Assembly*, *Library &
sweeps*. Selected ones:

| Setting | Default | What it does |
|---|---|---|
| `interactive_search_enabled` | on | Grab releases for missing albums. |
| `interactive_search_max_gb_per_album` | 2.5 | Cost ceiling per album filled. |
| `interactive_search_skip_placeholder_artists` | on | Never artist-search Various Artists. |
| `interactive_search_min_seeders` | 1 | Skip dead swarms. |
| `interactive_search_refuse_unofficial` | on | Refuse live/compilation/remix unless the album is one. |
| `strict_import_only` | on | Never move files into the library behind Lidarr's back. |
| `verify_library_after_import` | on | Confirm the album really landed. |
| `prefer_lossless_over_lossy` | on | Quarantine a lossy twin when a lossless exists. |
| `delete_source_folder_on_success` | off | Remove the download after a verified import. |
| `cue_ledger_enabled` | on | Remember each `.cue`'s verdict so a restart never re-splits. |
| `cue_ledger_max_attempts` | 3 | Retries before a failed cue is left for you. |
| `sweep_ledger_enabled` | on | Same for cueless folders. |

---

## Deploying a code change

The image is **built locally**; there is no registry. `docker restart` does
**not** pick up new code — a container is pinned to the image id it was created
from.

```bash
cd /mnt/user/appdata/cue_pipeline_src && git fetch -q origin && git reset -q --hard origin/main
rm -rf /tmp/cuebuild && mkdir -p /tmp/cuebuild
cp *.py /tmp/cuebuild/ && cp -r tools /tmp/cuebuild/
printf 'FROM cue_pipeline:latest\nRUN find /app -maxdepth 1 -name "*.py" -delete && rm -rf /app/tools\nCOPY *.py /app/\nCOPY tools/ /app/tools/\n' > /tmp/cuebuild/Dockerfile.inc
cd /tmp/cuebuild && DOCKER_BUILDKIT=1 docker build -f Dockerfile.inc -t cue_pipeline:latest .
docker rm -f cue_pipeline && bash /tmp/gen2.cmd
```

- A **full** `docker build` stalls (it re-downloads ffmpeg's ~200-package tree).
  Always build incrementally from `cue_pipeline:latest`.
- **Never use Unraid's Docker UI → Edit → Apply.** Apply tries to *pull* the
  local tag and deletes it. Recover by retagging the dangling image.
- The WebUI takes 30–60 s to bind. Poll until 200 before calling it broken.
- After every recreate: mounts non-empty, `--user 99:100`, the dockerman label,
  WebUI 200, zero tracebacks.
- Editing the inline JS in `webui.py`? `py_compile` proves nothing about it —
  extract the `<script>` blocks and `node --check` them.

---

## State files

All under `/config`, all safe to delete (they rebuild).

| File | Holds |
|---|---|
| `cue_seen.json` | Per-`.cue` verdict + attempts, so a restart never re-splits. |
| `sweep_seen.json` | Cueless folders already handled. |
| `convert_queue.json` | Pending conversions, so a deploy doesn't lose them. |
| `held_items.json` | The Needs-attention list. |
| `assembly.json` | Album-assembly plans. |
| `interactive_search.json` | Per-album missing clock, cooldowns, blocklisted guids. |
| `library_audit.csv` | Last audit report. |
| `external_album_audit.json` | Albums MusicBrainz has that Lidarr doesn't. |
| `ledger.csv` | Every outcome, appended. |
| `pipeline.log` | Rotating log (5 MB × 4). |

---

## HTTP API

```
GET  /api/held            /api/assembly        /api/progress
GET  /api/settings        /api/log             /api/tree
GET  /api/library/ls      /api/library/tracks  /api/audio/info
GET  /api/convert/options /api/convert/folders
POST /api/held/keep|move|discard
POST /api/assembly/add|find|comp|remove
POST /api/convert/start|pause|resume|cancel|cleardone|mkdir
POST /api/settings        /api/log/clear
POST /api/restart         /api/shutdown
GET  /healthz
```

---

## Troubleshooting

**An album reads 0/N but the files are right there.** The audit's
under-registered check imports them by explicit track id. If it hasn't yet, its
pass runs every `library_audit_interval_seconds`.

**A cue splits over and over.** It shouldn't — `cue_seen.json` records each
verdict. If it does, check whether the `.cue` file's size/mtime is changing.

**Settings save but nothing changes.** The three-edit trap above.

**Lidarr shows fewer tracks than the folder holds.** Under-registration. It also
makes the interactive search treat those tracks as missing and re-download them.

**A torrent downloads albums you already own.** Check the deselect's log lines:
`HAVE ... -> deselect` versus `KEEP ... (not in library)`. A `KEEP` for
something you own means the artist couldn't be resolved from the folder name.

**Conversions are slow while the pipeline works.** By design — the converter
yields.

---

## Layout

```
main.py             entry point, threads, config assembly
orchestrator.py     the passes, import strategies, audit, matching
lidarr.py           Lidarr API client + name normalisation
qbt_deselect.py     discography trimming
dedup_downloads.py  library-completeness checks
converter.py        library tree + transcoder
assembly.py         album assembly planner
splitter.py         cue splitting
cue_parser.py       cue parsing (+ optional LLM repair)
song_harvest.py     per-track harvesting
musicbrainz.py      MusicBrainz client (aliases, release groups)
prowlarr.py         indexer search
webui.py            the web UI and HTTP API
```
