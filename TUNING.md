# Tuning guide — grabbing, quality & cleanup

How to control **what the pipeline grabs, how aggressively, and how it cleans up
dead/lossy junk**. Every setting is available in the Unraid template (Edit the
container) as an env var, or in `config.yaml`. Defaults shown in **bold**.

---

## Quality: prefer lossless, lossy only as a last resort

The pipeline never *blindly* drops lossy — it grabs lossy **only when no
lossless release exists**, so you still fill gaps that are lossy-only.

| Env var | Default | What it does |
|---|---|---|
| `ISEARCH_REQUIRE_LOSSLESS` | **true** | Prefer lossless. Per-album search considers *all* releases and grabs the best available, with any lossless outranking any lossy. Set `false` for no format preference. |
| `PREFER_MULTICHANNEL` | **true** | In the conversion step (SACD/DVD-Audio/DSD), keep the multichannel version and discard the stereo one when a source ships both. |

> If you only want lossless and would rather leave a gap than take lossy, that's
> a Lidarr **Quality Profile** decision (Lidarr chooses what's allowed to grab);
> this pipeline's job is to prefer lossless among what Lidarr permits.

---

## Interactive search — how aggressively gaps get filled

The interactive search proactively grabs monitored albums Lidarr has left
missing. **This is the knob that controls the firehose.** With a large missing
backlog, aggressive settings flood qBittorrent with dead/lossy grabs.

| Env var | Default | Aggressive (fast backfill) | Gentle (less junk) | Notes |
|---|---|---|---|---|
| `ISEARCH_ENABLED` | false | true | true | Master switch. |
| `ISEARCH_DRY_RUN` | true | false | false | Keep `true` first to watch the log, then flip. |
| `ISEARCH_MIN_DAYS` | 3 | 0 | **3–7** | Days an album must be missing before we act. `0` = act on everything now. Higher = let Lidarr's own search try first. |
| `ISEARCH_MAX_ALBUMS` | 15 | 100 | **10–15** | Albums processed per pass (grouped by artist). Lower = fewer simultaneous grabs. |
| `ISEARCH_INTERVAL` | 3600 | 300 | 1800+ | Seconds between passes (min 300). |
| `ISEARCH_MAX_CANDIDATES` | 5 | 1000 | 5 | Releases tried+verified per album before giving up. |
| `ISEARCH_MIN_TITLE_RATIO` | 0.55 | 0.45 | 0.55 | Title-match floor; below it the album is skipped + flagged. Lower risks wrong grabs. |
| `ISEARCH_ARTIST_LEVEL` | true | true | true | Also grab discographies that fill several missing albums at once. |

**Recommended if you saw the "everything stuck at 0%" flood:** raise
`ISEARCH_MIN_DAYS` to `3` and drop `ISEARCH_MAX_ALBUMS` to `15`. Lidarr's own
auto-search gets first crack, and far fewer dead torrents pile up.

---

## Dead-grab reaper — clean up torrents that never start

Grabbing at scale inevitably pulls some dead releases (no seeders / no
metadata) that sit at 0% forever. The reaper removes them after a grace window
and **blocklists** them so Lidarr re-searches a live (lossless-preferred)
alternative.

| Env var | Default | What it does |
|---|---|---|
| `QBIT_DEAD_GRAB_REAPER` | **true** | Enable the reaper. |
| `QBIT_DEAD_GRAB_GRACE_HOURS` | **48** | A torrent must be stuck at ~0% this long (from when it was added) before removal. `48` = wait 2 days. |
| `QBIT_DEAD_GRAB_BLOCKLIST` | **true** | Blocklist the removed release in Lidarr so it grabs a different one instead of re-grabbing the same dead torrent. |

Only `lidarr`-category torrents that are **at ~0% in a dead/stalled/metadata
state** are touched. **Paused/stopped** torrents (a deliberate "don't
download") and anything that made real progress are always left alone.

---

## Video exclusion (#4)

| Env var | Default | What it does |
|---|---|---|
| `QBIT_DESELECT_VIDEO` | **true** | Deselect video from `lidarr`-category torrents so a music grab never pulls a concert DVD/BD: drops `VIDEO_TS`/`BDMV` zones and standalone video containers (mkv/mp4/ts/…). A DVD-Audio `AUDIO_TS` zone and a whole-disc `.iso` are **audio** and kept. |

---

## Archive & optical-disc extraction

| Env var | Default | What it does |
|---|---|---|
| `EXTRACT_ARCHIVES` | **true** | Unpack `.rar/.zip/.7z/.tar[.gz]`/multi-part-rar downloads in place with 7z, then import the audio normally. |
| `EXTRACT_SACD_ISO` | **true** | Rip a ScarletBook SACD `.iso` to multichannel FLAC (`sacd_extract`). |
| `EXTRACT_DVDA_ISO` | **true** | Rip a DVD-Audio `.iso` (AUDIO_TS/.AOB) to multichannel FLAC (`dvda2wav` + 7z). |
| `TRANSCODE_DSD` / `TRANSCODE_DTS_CD` | **true** | DSD → 48k/16 FLAC; DTS-CD → 5.1 FLAC. |

Extracted archives/ISOs are **kept until the source folder is deleted after a
verified import**, so a failed import never loses the original.

---

## Content-identify — import albums whose folder name doesn't match Lidarr

When a downloaded album can't be matched to a Lidarr album **by name** (artist
dumps like `Hans Zimmer/Gladiator` vs Lidarr's "Gladiator: Music From the
Motion Picture"), the pipeline identifies it by its **content**: the download's
track titles (embedded tags → informative filenames → a tracklist scraped from
a sidecar `.nfo`/`.txt`) are fuzzy-matched against the track lists of the
artist's monitored albums — **including non-selected releases**, so a classic
17-track pressing hidden behind a selected anniversary edition still matches.
Runs in the cueless sweep's handoff **and** as a reconcile-pass fallback.

Accuracy first: it refuses when coverage is below the floor, when a rival album
matches almost as well, or when the source is marked live/demo/remix and the
album title isn't (same titles, different recording). Ambiguous or title-less
folders can be sent to the LLM for a pick, which is then verified against the
album's release track counts before anything is imported.

| Env var | Default | What it does |
|---|---|---|
| `CONTENT_IDENTIFY` | **true** | Enable content-based album identification. |
| `CONTENT_IDENTIFY_MIN_COVERAGE` | **60** | Min % of the download's titled files that must map to distinct tracks of ONE album. |
| `CONTENT_IDENTIFY_LLM` | **true** | Allow the LLM confirmation step for ambiguous/title-less folders. |

---

## WebUI — see & resolve what's stuck

Open the container's **WebUI** (port `8830`). Two tabs:

- **Needs attention** — a dense, filterable table of items the pipeline
  couldn't finish (outcome `failed`, files still on disk). Each row shows the
  audio profile (formats, lossless/lossy, channels, sample-rate/bit-depth,
  size). Filter by text or condition chips (lossless / has-lossy / multichannel
  / stereo / by-outcome), toggle which detail columns show, then per item:
  **Copy path**, **Keep existing** (discard the held files, trust Lidarr's
  library) or **Move held** (move the files into the library and rescan).
- **In progress** — live conversions (SACD / DVD-Audio / DTS / DSD / archive).

| Env var | Default | What it does |
|---|---|---|
| `WEBUI_ENABLED` | **true** | Serve the dashboard. |
| `WEBUI_PORT` | **8830** | Port (publish it on the container: the template already maps `8830`). |

**Right-click** any cell in the table for ServiceNow-style **Show matching** /
**Filter out** on that value; active value-filters show as removable chips.
The **Existing (library)** and **Compare** columns show what Lidarr already has
for the album next to the held version (e.g. "held better ↑" when the held rip
is lossless/multichannel and the library copy is lossy/stereo), so you can
decide **Keep existing** vs **Move held** at a glance.

---

## File permissions (delete from Windows/SMB)

The container runs as `--user 99:100` (nobody:users) — **required**, or Lidarr
can't import pipeline-created files (root-owned files fail with a copy/delete
retry loop). To also let *you* delete/modify those files over SMB, the pipeline
sets its umask to `0` so everything it (and ffmpeg/7z/sacd_extract/dvda2wav)
creates is `0666`/`0777` — group- and other-writable, matching Unraid's
"Docker Safe New Permissions".

| Env var | Default | What it does |
|---|---|---|
| `FILE_UMASK` | **0** | Octal umask for created files. `0` → 0666/0777 (anyone can delete). `002` → group-writable only. |

For files created **before** this was in place, run Unraid **Tools → Docker
Safe New Permissions** on the share (or `chmod -R 666 files / 777 dirs`). Do
**not** remove `--user 99:100` to fix permissions — that breaks Lidarr imports.
