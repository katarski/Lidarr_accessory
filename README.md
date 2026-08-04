# Lidarr Accessory (`cue_pipeline`)

A companion service for **Lidarr** that deals with everything Lidarr itself
won't: disc images, exotic formats, discography torrents, albums whose folder
name matches nothing, and gaps that sit missing forever. It runs as a container
next to Lidarr (or as a Windows service) and has its own web UI on port 8830.

What it does, in the order you notice it:

- **Imports what Lidarr can't.** Splits `.cue` disc images with ffmpeg, decodes
  DTS-CD and DSD, rips SACD and DVD-Audio ISOs to multichannel FLAC, unpacks
  archives, transcodes APE/WavPack/ALAC/WAV to FLAC, and identifies an album by
  its **track titles** when the folder and tags say nothing useful.
- **Finds music Lidarr left missing.** Searches per **album** first; only when an
  album cannot be found on its own does it fall back to the artist scope and
  accept a discography or box set that contains it. Every candidate is verified
  inside qBittorrent before it counts — track counts, song titles, seeders — and
  a bad one is blocklisted rather than retried.
- **Keeps discography torrents lean.** De-selects albums you already own (whole
  folders, not just audio), refuses compilations / live / remix / bootleg
  releases unless the album really is that kind of record, and removes a
  completed torrent once every album it carried is in the library.
- **Assembles albums that exist only in pieces.** Works out which songs of a
  missing album are sitting inside compilations you already have, shows the
  percentage assembled, and can hunt torrents for just the missing songs.
- **Hands you the rest.** Anything it will not decide alone lands in
  **Needs attention** with the evidence, for one click.

Ollama (or any OpenAI-compatible endpoint) is optional. When enabled it repairs
malformed CUE files the deterministic parser can't handle and helps choose
between candidate albums; when it is off or unreachable every deterministic path
still works.

## Web UI (port 8830)

| Tab | What it's for |
|---|---|
| **Needs attention** | Albums that need a decision, with track-level comparison against the library. Add to library / Overwrite / Discard, in bulk or per row. |
| **Assembly** | Missing albums that can be built from songs you already have: percentage assembled, which songs are still missing, and Find missing / Add to library / Remove. |
| **Progress / Converter** | Live conversion and cue-split progress, plus a library browser that transcodes to MP3/AAC/Opus — optionally replacing the original in place and repointing Lidarr at it. |
| **Log** | The pipeline log and Lidarr's flac2mp3 downsampler log. |
| **Settings** | Every tunable, grouped by category, each with a recommended value and a per-section **Recommended** button. Saved to `webui_overrides.json` (highest precedence) and applied on restart. |

There's a built-in **audio player** throughout: click a song to play it, click a
folder to queue it recursively in explorer order, with a numbered playlist,
shuffle, repeat (folder or single track), ID3 tags and codec specs. Clicking a
single song and pressing Next pulls in the rest of that folder.

## Install on Unraid (Docker) — recommended

Running it as a container next to Lidarr (same `/downloads` + `/music`
namespace) is the primary supported setup. Full step-by-step guide:
**[UNRAID_SETUP.md](UNRAID_SETUP.md)**.

TL;DR (Unraid web terminal, `>_` icon):

```sh
git clone https://github.com/katarski/Lidarr_accessory.git /mnt/user/appdata/cue_pipeline_src
cd /mnt/user/appdata/cue_pipeline_src && docker build -t cue_pipeline:latest .
mkdir -p /mnt/cache/appdata/cue_pipeline
cp config.example.yaml /mnt/cache/appdata/cue_pipeline/config.yaml   # then edit it
```

Then Docker tab → **Add Container** → paste this in the **Template** field:

```
https://raw.githubusercontent.com/katarski/Lidarr_accessory/main/cue_pipeline.xml
```

Point the `/downloads` and `/music` mounts at the same host paths Lidarr uses,
fill in your Lidarr API key, and **Apply**. On Unraid the container also:

- **de-selects** albums you already own from discography torrents in qBittorrent
  (whole folder, not just audio) — pausing new torrents so nothing leaks;
- **manages completed torrents** — pauses a torrent while it's mid-import and
  removes it (with data) once every album has moved into the library.

## Files

| File | What it does |
|---|---|
| `main.py` | Entry point. Loads YAML config, starts watchdog, runs worker. |
| `orchestrator.py` | Per-CUE state machine. |
| `cue_parser.py` | Deterministic CUE parser + Ollama repair fallback. |
| `splitter.py` | ffmpeg invocation (lossless FLAC out). |
| `tagger.py` | mutagen Vorbis-comment tagging. |
| `lidarr.py` | Lidarr API client (scan, import, refresh). |
| `ollama_client.py` | Ollama HTTP client (repair CUE, normalize tags). Named to avoid shadowing `ollama.exe` on Windows PATH. |
| `qbt_deselect.py` | qBittorrent selective download: de-select owned albums, adopt uncategorised music torrents, reap stalled/dead grabs, completed-torrent lifecycle. |
| `qbittorrent_client.py` | qBittorrent Web API client. Verifies the effect it asked for (qBit answers 200 for a hash it has never heard of). |
| `webui.py` | The web UI on port 8830 and its JSON API (needs attention, assembly, converter, settings, audio streaming). |
| `assembly.py` | Song-to-track matching for building an album out of compilations. |
| `converter.py` | Library browser + MP3/AAC/Opus conversion, optional replace-in-place. |
| `held_store.py` | File-backed needs-attention store, so the page loads instantly. |
| `dedup_downloads.py` | Removes duplicate/redundant copies inside a download. |
| `musicbrainz.py` | Read-only MusicBrainz client for cross-checking albums Lidarr never listed. |
| `acoustid_client.py` | AcoustID fingerprinting for rips with no usable tags. |
| `tools/tpl2run.py` | Prints the `docker run` the Unraid template describes — do not retype mounts by hand. |
| `config.yaml` | All tunables — paths, URLs, API keys, model name. |
| `requirements.txt` | Python deps. |

## Install (Windows) — easy path

Copy this whole folder to the RTX 3090 box (e.g. `C:\Tools\cue_pipeline`)
and double-click **`install.bat`**. It will:

1. Check Python 3.11+ and ffmpeg/ffprobe are on PATH.
2. Create `.venv` and install Python deps.
3. Pull `qwen2.5:32b` via `ollama pull` (~19 GB; one-time download).
4. Print a checklist of config fields you still need to fill in.

Prereqs the installer expects already present on the machine:

- **Python 3.11 or 3.12** (tick "Add to PATH" in the installer).
- **ffmpeg + ffprobe** on PATH — grab a static build from
  <https://www.gyan.dev/ffmpeg/builds/>, unzip to e.g. `C:\ffmpeg`, add
  `C:\ffmpeg\bin` to PATH.
- **Ollama** on PATH if you want the installer to pull the model here.
  If Ollama lives on a *different* box, point `ollama.base_url` at it in
  `config.yaml` and run `ollama pull qwen2.5:32b` on that box.

## Install (Windows) — manual path

```powershell
python --version       # must be 3.11+
ffmpeg  -version
ffprobe -version

cd <this folder>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# On the Ollama host (may or may not be this machine):
ollama pull qwen2.5:32b
```

## Configure

Edit `config.yaml`:

- `watch.root` — the download folder (`V:/Dan/Internet Downloads` or the UNC).
- `staging.root` — where tracks are staged before import. Keep on the same
  volume as `watch.root` so moves stay atomic.
- `ffmpeg.binary` — leave as `"ffmpeg"` if it's on PATH; otherwise full path
  to `ffmpeg.exe`.
- `lidarr.base_url` — e.g. `http://192.168.1.50:8686`.
- `lidarr.api_key` — from Lidarr → Settings → General → Security → API Key.
- `lidarr.library_root_lidarr` — the music root as Lidarr sees it inside its
  Docker container (e.g. `/music`).
- `lidarr.library_root_windows` — the same folder as Windows sees it
  (e.g. `\\PARK\Music`).
- `lidarr.path_mapping.from/to` — Windows → Lidarr translation for the
  **staging** path. Pick any folder that is mounted in both.
- `ollama.base_url` — `http://127.0.0.1:11434` if you run this script on the
  same box as Ollama. If the script runs on a different machine, point it at
  the Ollama host.
- `ollama.model` — any instruct model you have pulled. `qwen2.5:14b` or
  `llama3.1:8b` are fine starting points.

### Lidarr path mapping cheat sheet

Say your staging is `V:/Dan/Internet Downloads/_split_staging/SomeAlbum`.
Lidarr (in Docker) probably has that volume mounted too, just under a
different path. Find the mapping in Unraid's Lidarr docker settings:

- Windows sees: `V:/Dan/Internet Downloads/`
- Docker mount inside Lidarr: `/downloads/dan/` (example)

Then in `config.yaml`:

```yaml
lidarr:
  path_mapping:
    from: "V:/Dan/Internet Downloads"
    to:   "/downloads/dan"
```

## Run interactively

Double-click **`run.bat`** (or from a prompt):

```powershell
run.bat
```

Under the hood that's just `.\.venv\Scripts\python main.py --config config.yaml`.
Drop a `.cue` + `.flac/.ape/.wv` into the watched folder. You should see
log lines in the console and the rotating log file. Ctrl+C to stop.

## Run as a Windows service with NSSM

Drop `nssm.exe` somewhere on PATH (get it from <https://nssm.cc/download>,
the `win64` build), open an **elevated** cmd / PowerShell, and run:

```powershell
install_service.bat
```

It wires up the service, sets auto-start, redirects stdio to log files,
and starts it. To tear it down later:

```powershell
uninstall_service.bat
```

## Processing flow (the cue-image path)

This is the original path, kept because it is the fiddliest. Loose tracks, ISOs,
archives and Lidarr gaps take shorter routes to the same import step.


```
new .cue appears
  │
  ▼
wait for .cue + audio to be size-stable for `stable_seconds`
  │
  ▼
ffprobe audio to get duration (fills last track's end time)
  │
  ▼
parse CUE deterministically  ──(fail?)──►  Ollama repair  ──►  parse again
  │
  ▼
ffmpeg -ss/-to  →  NN - Title.flac  (one per track, lossless FLAC)
  │
  ▼
mutagen tags  ──(optional)──►  Ollama normalizes capitalization
  │
  ▼
POST /api/v1/command  DownloadedAlbumsScan  (path-mapped to Lidarr's view)
  │
  ▼
wait up to `lidarr_grace_seconds` for staging to clear
  │
  ├── staging empty? ──►  done. Park original disc image under _processed/.
  │
  └── still full? ──►  move tracks into <library>/<Artist>/<Artist - Year - Album>/
                       POST RefreshArtist so Lidarr picks them up.
                       Park original disc image.
```

## Troubleshooting

- **"ffprobe failed"** — ffmpeg and ffprobe must both be on PATH. A stray
  static ffmpeg without ffprobe is the usual culprit.
- **"Path X is not under mapped prefix"** — `lidarr.path_mapping.from` must
  be the Windows prefix of your staging folder. Both are normalized to
  forward slashes; match case is ignored but spelling isn't.
- **Lidarr doesn't import** — verify the API key, then check Lidarr's
  Activity → Queue/History. Usually the release name doesn't match any
  artist in your library; the manual-move fallback handles that case but
  requires the artist to already exist in Lidarr for the RefreshArtist
  call to work.
- **Ollama slow** — set `ollama.enabled: false` in config to skip LLM
  calls entirely. The deterministic parser handles most real-world CUEs.
- **Weirdly encoded CUE files** — the parser tries UTF-8/CP1251/CP1252/
  Shift-JIS/GB18030/Latin-1 before falling back to chardet. If parsing
  still fails, Ollama gets a shot. If Ollama is off, you'll see a
  `ValueError` in the log — open the CUE, fix encoding, save as UTF-8.

## What this does NOT do (on purpose)

- **No CUE Splitter v2.0.8 / shntool.** ffmpeg handles FLAC/APE/WV/WAV
  directly. shntool was explicitly rejected (artifacts).
- **No MusicBrainz lookups.** Lidarr already does that; we stay out of it.
- **No aggressive LLM use.** Ollama only runs when the CUE is malformed
  or when tag normalization is explicitly requested.

(On Unraid the container *does* talk to qBittorrent — selective-download and
completed-torrent lifecycle — see [UNRAID_SETUP.md](UNRAID_SETUP.md). The
standalone Windows watcher above does not.)
