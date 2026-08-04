# cue_pipeline

A Lidarr companion that turns messy music downloads into a clean library, and
gives you a WebUI to watch and steer it.

It started as a `.cue` disc-image splitter (that still works, and is still the
safest path for a single-file rip), but it now covers the whole awkward middle
ground Lidarr leaves alone:

* **Splits disc images** — FLAC/APE/WV/WAV + `.cue`, split with **ffmpeg** (no
  shntool, so none of its artifacts), tagged, then handed to Lidarr.
* **Imports folders Lidarr won't** — pre-split albums whose folder name doesn't
  match anything, artist dumps, box sets, multi-disc sets (all discs imported as
  ONE album), and albums identified from **track titles, tags, `.nfo`
  tracklists** or an **AcoustID fingerprint** when the tags are useless.
* **Rips optical images** — SACD ISO (`sacd_extract`), DVD-Audio ISO
  (`dvda2wav`, multichannel preferred), DSD → FLAC, DTS-CD → 5.1 FLAC, and
  unpacks `.rar/.zip/.7z` (with an `unrar` fallback, since p7zip silently writes
  0-byte files for some RARs).
* **Controls what actually downloads** — deselects albums you already own inside
  a discography torrent, refuses non-official material (greatest-hits, live,
  remix, karaoke…) unless Lidarr genuinely wants that record, prefers healthy
  swarms, and verifies a release's **song titles** before committing so a
  right-sized/wrong-album grab is rejected.
* **Cleans up after itself** — removes torrents with nothing left to want,
  reaps dead grabs and stalled downloads (salvaging any finished songs first),
  and never deletes data it hasn't confirmed is in your library.
* **Fills gaps** — a reconcile pass imports anything Lidarr still lists as
  missing but that is sitting in your downloads, and an **Assembly** view works
  out which songs of a missing album live inside the compilations you already
  have, then assembles them.
* **Cross-checks Lidarr** — asks MusicBrainz which studio albums an artist has
  that Lidarr never listed (read-only report).

Ollama is optional: it repairs CUE files the deterministic parser can't handle
and helps with ambiguous album matching. If it's unreachable everything still
works deterministically.

## WebUI (port 8830)

| Tab | What it's for |
|---|---|
| **Needs attention** | Everything the pipeline could not finish on its own, with the held copy vs your library side by side (quality, track count, folder trees) and per-row **Add to library / Overwrite / Discard**. Served from a file-backed store, so it loads instantly. |
| **Assembly** | Per missing album: **% assembled**, which songs were found and in which file, and which are still missing. Actions: *Find missing* (hunts collections for the songs, repeating across releases until found), *Add to library* (copies, retags to the target album, imports), *Remove*. |
| **Converter** | Browse the library, convert to MP3/AAC/Opus with dbpoweramp-style options, live per-file and total progress. Optional **Overwrite existing** replaces the source after a verified encode, repoints sidecar `.xml`, and tells Lidarr. |
| **Log** | Tail `pipeline.log` or the Lidarr flac2mp3 downsampler log. |
| **Settings** | The curated tunables, applied without editing YAML. |

Any music file in those tabs is **clickable** — a small player opens at your
pointer with ID tags, specs, transport controls and volume. `Esc` closes it.

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
| `main.py` | Entry point: loads YAML + env + WebUI overrides, starts every loop/thread. |
| `orchestrator.py` | The brain — per-CUE state machine, cueless sweep, ISO/DSD/DTS/archive handling, Lidarr handoff, reconcile, assembly planning, held-item curation. |
| `cue_parser.py` | Deterministic CUE parser + Ollama repair fallback. |
| `splitter.py` | ffmpeg invocation (lossless FLAC out) with per-track progress. |
| `tagger.py` | mutagen Vorbis-comment tagging. |
| `lidarr.py` | Lidarr API client (search, grab, manual import, queue, rescan). |
| `qbt_deselect.py` | qBittorrent selective download: deselect owned/non-official albums, video exclusion, dead-grab + stalled-download reapers, completed-torrent lifecycle. |
| `qbittorrent_client.py` | qBittorrent Web API client. |
| `dedup_downloads.py` | "Do we already own this album?" matching + duplicate-download purge. |
| `assembly.py` | Album assembly: song-level matching of compilations against missing albums, and the plan store behind the Assembly tab. |
| `musicbrainz.py` | Read-only MusicBrainz client for the "albums Lidarr never listed" audit. |
| `acoustid_client.py` | fpcalc + AcoustID fingerprint identification (for untagged rips). |
| `converter.py` | Library tree cache + the MP3/AAC/Opus conversion engine. |
| `held_store.py` | File-backed store behind the Needs-attention tab. |
| `webui.py` | The WebUI: pages, JSON API, audio streaming for the player. |
| `ollama_client.py` | Ollama HTTP client. Named to avoid shadowing `ollama.exe` on Windows PATH. |
| `cloud_llm.py` | Optional cloud LLM fallback. |
| `config.yaml` | All tunables — paths, URLs, API keys, model name. |
| `TUNING.md` | What every knob does and when to change it. |
| `requirements.txt` | Python deps. |

### State files (in `/config`)

`pipeline.log`, `held_items.json` (needs-attention), `assembly.json` (assembly
plans), `sweep_seen.json` (persistent sweep ledger — stops a restart replaying
the whole queue), `library_tree.json` (Converter cache),
`interactive_search.json`, `external_album_audit.json` (MusicBrainz gaps),
`webui_overrides.json` (Settings tab; **highest precedence**, so check here if a
value looks stuck).

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

## Processing flow (what actually happens)

There are several entry points; the `.cue` path below is the original one.

| Trigger | Path |
|---|---|
| A `.cue` appears (watcher, or the periodic re-scan that catches ones the watcher dropped on SMB) | split → tag → Lidarr, as diagrammed below |
| A folder of already-split audio, no `.cue` | cueless sweep → identify (tags → folder → track titles → `.nfo` → AcoustID) → Lidarr ManualImport. Folders that still need identifying go **first**; multi-disc sets are imported as ONE album |
| An ISO / archive | SACD or DVD-Audio rip, or 7z/unrar unpack, then re-enters the sweep as ordinary audio |
| A torrent is added | deselect owned + non-official albums, drop video, keep songs an assembly needs |
| A torrent completes | lifecycle: remove it once every album Lidarr *wants* from it is owned |
| Periodically | reconcile (import gaps still in downloads), assembly planning, needs-attention curation, MusicBrainz audit, stalled/dead-grab reaping |

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
- **No aggressive LLM use.** Ollama only runs when the CUE is malformed, when
  an album match is genuinely ambiguous, or when tag normalization is asked for.
- **No writing to Lidarr's metadata.** The MusicBrainz cross-check is
  **read-only** — it reports albums Lidarr never listed, it never adds them,
  because adding an album changes what the whole pipeline then chases.
- **No deleting data it hasn't confirmed.** A torrent's files are never removed
  unless at least one of its albums is verified present in your library; a
  stalled download is salvaged before anything is discarded.

Two claims that used to be here are no longer true:

- ~~No MusicBrainz lookups~~ — it *does* query MusicBrainz now (read-only,
  ≤1 req/s, see `musicbrainz.py` and the audit knobs in
  [TUNING.md](TUNING.md)), because Lidarr's library only holds what it decided
  to add and can simply be missing an album.
- ~~Windows-only~~ — the Unraid container is the primary target. It talks to
  qBittorrent (selective download, reapers, lifecycle) and serves the WebUI; see
  [UNRAID_SETUP.md](UNRAID_SETUP.md). The standalone Windows watcher still works
  but only does the split-and-hand-to-Lidarr part.
