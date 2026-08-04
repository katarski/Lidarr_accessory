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

## Official releases only + swarm health (grabs)

| Knob | Default | Effect |
|---|---|---|
| `lidarr.interactive_search_refuse_unofficial` | `true` | Refuse a release whose title advertises greatest-hits / best-of / anthology / collection / box set / live / unplugged / remix / DJ-mix / mixtape / karaoke / tribute / bootleg / b-sides / rarities material — **unless the Lidarr album being filled is itself that kind of record** (by its albumType, secondaryTypes or its own title). So a missing "Live Rust" still downloads; a studio album never gets a hits package instead. |
| `lidarr.interactive_search_min_seeders` | `1` | Never grab a release with fewer seeders. A 0-seeder grab is not a download — it is what produces torrents stuck at 0%. |
| `lidarr.interactive_search_seeder_weight` | `1.0` | How strongly seeders (+ leechers at 0.3×) weigh in ranking. `1.0` makes swarm health comparable to title relation, so among releases of the *right* album the healthiest wins. `0` ignores it. |

The same "not an official studio album" test is applied to **folders inside a
discography torrent** and to an **album folder's own name**, so a folder literally
called `... All Their Greatest Hits 2001` is deselected by default instead of
downloading just because Lidarr has no record of it.

## Song-title verification before a grab

| Knob | Default | Effect |
|---|---|---|
| `lidarr.verify_track_titles` | `true` | Compare Lidarr's track titles against the songs inside the torrent (its file names) before committing. |
| `lidarr.verify_track_titles_accept` | `0.60` | Coverage at or above which the release is accepted **even if the file count differs** (bonus tracks, another pressing). |
| `lidarr.verify_track_titles_reject` | `0.25` | Coverage below which it is rejected as a different record **even when the counts line up**. |

Coverage is measured **per release** and the best one wins: pooling every
release's titles badly understates a correct grab (an album with 10 releases and
38 distinct titles scored 34% for a perfectly correct 11-track torrent).

## Reconcile — import gaps still sitting in downloads

| Knob | Default | Effect |
|---|---|---|
| `lidarr.reconcile_enabled` | `true` | Import anything Lidarr still lists as missing but which is present in your downloads, using Lidarr's own matcher rather than folder-name heuristics. |
| `lidarr.reconcile_interval_seconds` | `1800` | How often it runs. |
| `lidarr.reconcile_require_full_album` | `true` | Only import when the source supplies **every** track of the album (track-level source↔destination match). Off allows partial fills. |
| `lidarr.reconcile_import_mode` | `copy` | `copy` leaves the download in place (safe while seeding); `move` consumes it. |

## Needs attention — automatic resolution

| Knob | Default | Effect |
|---|---|---|
| `lidarr.held_auto_resolve` | `true` | Let logic settle what it can: drop duplicate rows an ancestor row already covers (a box set recorded at several levels), and auto-import rows that are simply monitored Lidarr gaps. Only fires when the held folder holds **exactly** the album's whole track list, so partial/oversized matches stay for you. |
| `lidarr.webui_held_refresh_seconds` | `300` | Background curator cadence (prune, box-set expand, library compare, auto-dismiss). The page itself always serves the store as-is. |

## Album assembly (build a missing album from compilations)

| Knob | Default | Effect |
|---|---|---|
| `lidarr.assembly_enabled` | `true` | Work out which songs of a missing album sit in the compilations stuck in needs-attention. |
| `lidarr.assembly_interval_seconds` | `1800` | Planner cadence (first pass ~45s after start). |
| `lidarr.assembly_min_score` | `0.87` | Title-match score a song must reach to count as found. |
| `lidarr.assembly_require_artist` | `true` | The source's own artist evidence must agree, so a cover of the same song on a tribute compilation is not counted. Folder names may only *confirm*, never reject; a file with no artist evidence is judged on title alone. |
| `lidarr.assembly_min_pct` | `10` | Don't keep a plan below this % assembled. |
| `lidarr.assembly_max_albums_per_pass` | `150` | Albums planned per pass (each costs a Lidarr track-list request); the rest rotate in next pass. |
| `lidarr.assembly_hunt_per_pass` | `2` | Active *Find missing* hunts given one grab-and-verify attempt per pass. |

**Find missing** deliberately inverts the grab rules: it searches the artist's
whole release list (where collections live), allows non-official releases, and
skips releases for albums you already own. A candidate is accepted only if its
file list really carries a missing song; if not it is removed and blocklisted and
the next release is tried, repeating until the song is found. An accepted torrent
is narrowed **while still paused**, so it downloads only the needed songs — the
keep-set is the union across **all** assemblies, so a song another assembly wants
is never dropped.

## Stalled downloads (partly downloaded, no progress)

| Knob | Default | Effect |
|---|---|---|
| `qbittorrent.stalled_reaper` | `true` | Deal with a torrent that has real progress but hasn't moved in N days (from qBittorrent's own `last_activity`). |
| `qbittorrent.stalled_grace_days` | `3` | How long without progress before acting. |
| `qbittorrent.stalled_blocklist` | `true` | Blocklist a trashed release so Lidarr grabs a different one. |

**Salvage first:** any *complete* audio file that an assembly needs, or whose
album Lidarr still wants, means the **torrent only** is removed and the data is
KEPT for import. Only when nothing is salvageable are the torrent and its data
deleted. Torrents at 0% are the dead-grab reaper's job; paused/stopped torrents
are never touched.

## Re-checking torrents already assessed

| Knob | Default | Effect |
|---|---|---|
| `qbittorrent.redeselect_recheck_seconds` | `1800` | Re-plan a still-downloading torrent this often, so albums that become owned *after* it was first assessed get deselected too. `0` = plan once only. |

Newly added torrents are always handled **first**, so a fresh grab is narrowed
before a fast swarm can pull the whole thing.

## Persistent sweep ledger

| Knob | Default | Effect |
|---|---|---|
| `watch.sweep_ledger_enabled` | `true` | Remember which folders the cueless sweep already handed off, keyed on a content signature (audio count + newest mtime), so a restart doesn't replay an hour-long queue. |
| `watch.sweep_ledger_ttl_seconds` | `86400` | Re-examine an unchanged, already-handled folder after this long, so a transient failure is retried rather than written off. |

A folder is re-examined whenever its audio changes, the entry expires, or the
ledger is disabled. Delete `/config/sweep_seen.json` to force a full pass.
This also records ripped optical images, which is what stops a DVD-Audio ISO
being re-ripped forever after its extracted folder is cleaned up.

## Cross-check Lidarr against MusicBrainz

| Knob | Default | Effect |
|---|---|---|
| `lidarr.external_audit_enabled` | `true` | Ask MusicBrainz which **studio** albums an artist has that Lidarr has no record of, and report them to `/config/external_album_audit.json`. |
| `lidarr.external_audit_interval_seconds` | `21600` | Cadence (6h). |
| `lidarr.external_audit_artists_per_pass` | `10` | Artists per pass — MusicBrainz asks for ≤1 request/second, so the library rotates through over days. |
| `lidarr.external_audit_recheck_seconds` | `604800` | Don't re-check the same artist more often than this. |
| `lidarr.external_audit_include_eps` | `false` | Also treat EPs as expected releases. |

**Read-only on purpose** — nothing is added to Lidarr, because adding an album
changes what the whole pipeline then chases and downloads. Note MusicBrainz's own
typing is imperfect: a few flagged entries are bootlegs it typed as plain
"Album", so treat the report as advisory.

## Untagged rips (AcoustID)

Set `acoustid.enabled: true` and your own free key from
<https://acoustid.org/new-application> (the bundled public test key is dead and
returns `invalid API key`). Fingerprinting recovers artist/album for a rip whose
tags are useless — including one whose *filename* fallback produced a placeholder
identity like `artist='01' album='Track 01'`, which previously counted as
"identified" and skipped the lookup entirely. Folders that still need identifying
are handed off **first** in each sweep, so their lookup isn't stuck behind a long
queue.

## Converter

`Overwrite existing` replaces the source with the converted file: the encode goes
to a hidden temp name, and only after it is verified (exit 0, non-empty) is the
original **deleted**, sidecar `.xml` files repointed at the new name, and Lidarr
asked to rescan the album folder. A failed encode leaves the original untouched.
Folder ticks include everything beneath them; the affected folder is re-read
immediately after a conversion or delete, so the tree never lies.
