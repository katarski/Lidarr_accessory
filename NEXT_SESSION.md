# cue_pipeline — handoff for the next session

Written 2026-08-05. Everything below is DEPLOYED and running unless marked TODO.

## What was built this session: find-the-compilation

For a track that exists only on compilations, work out *which* compilation
carries it BEFORE searching for anything, then find that compilation on the
indexers. The old song hunt did the reverse — search by artist, grab whatever
ranked highest, and only discover the track list after downloading it.

The chain, as built:

1. Missing tracks (title + duration) come from Lidarr — `hasFile == false`.
2. **MusicBrainz** is asked which releases contain each recording, and the
   answers are pooled so a release is ranked by HOW MANY of the missing songs
   it carries. For `Billie Holiday / The Love Songs` the top answer covers 27
   of 34; for `Janis Joplin / Pearl` the top answers cover all 9.
3. Those compilation titles are searched for BY NAME on the indexers.
4. Results go into the existing rank → grab → verify → harvest path, which
   narrows the torrent to just the needed files.

Entry points: the **Find compilation** button per row in the Assembly tab, the
"Find the compilation" bulk action, or `assembly_find_compilation(album_id)`.
It runs a slice per invocation (3 titles, 1 grab) and persists state in the
plan under `comp`, so the periodic assembly pass carries it on and a restart
does not lose it. `_assembly_continue_hunts` runs `comp` BEFORE `hunt`.

### Why Prowlarr, and why that is not a wider net

**Lidarr cannot do step 3.** `GET /api/v1/release` accepts only an `albumId` or
`artistId` and builds the query from its own metadata — there is no free-text
parameter, and every compilation worth finding here is a title Lidarr has never
heard of. Prowlarr's `/api/v1/search` does take free text.

Prowlarr is used strictly as the TRANSPORT. Prowlarr has 13 indexers enabled
against Lidarr's 3 with music categories, and that gap is deliberate curation —
the extra trackers return noise (a search for the compilation "The Lady" came
back topped by a Lady GaGa album). So `indexer_ids_from_lidarr()` reads which
Torznab indexers Lidarr actually has switched on and pins every search to that
set: here `[1, 2, 12]` = RuTracker, The Pirate Bay, Xtreme Bytes. Same
indexers Lidarr would have queried; only the query shape is new.
`comp_hunt_lidarr_indexers_only=False` would widen it, and the noise with it.

Prowlarr's base URL is auto-detected from Lidarr's own indexer definitions
(their baseUrls point at Prowlarr). **The api key cannot be** — Lidarr masks it
as `********` when reading indexers back — so `prowlarr_api_key` must be set in
Settings or the feature refuses to run.

### Traps found the hard way in this workflow

- **Lidarr's `foreignRecordingId` is a dead end.** Every track carries one and
  it looks like the exact answer, but MusicBrainz holds a SEPARATE recording
  entry per release for these old sides. Browsing `/release?recording=<mbid>`
  for `Gloomy Sunday` returned **1** release — the album we already knew. The
  fuzzy `/recording?query=` search with `arid:` returned **125**. Use the
  search; the exact id is worthless here.
- **The recording search is fuzzy and scores partial matches** — "Sugar" pulls
  in "Sugar Blues". Titles are compared normalized-exact, and duration (±10s)
  is what separates a 1930s studio side from a later live take. A recording MB
  has no length for is KEPT: these rarities are where metadata is thinnest.
- **A self-titled compilation makes a poisonous search term.** "Billie Holiday"
  is a real MB compilation title, and searching for it IS the blind artist-level
  search this feature replaces. Titles matching the artist name are dropped.
- **Shortening a title is required, and it lies.** Full MB titles mostly return
  nothing ("The Quintessential Billie Holiday, Volume 9: 1940-1942" → 0 hits;
  drop the year range → exact hit). But an unanchored short title is dangerous:
  "The Lady" alone returned 72 results headed by Lady GaGa. So the ARTIST NAME
  IS PREFIXED TO EVERY VARIANT, and results are then scored against the full
  compilation title with `title_plausible()` — an F1 over identity tokens,
  scored both ways because one direction alone is fooled ("The Great Billie
  Holiday" reduces to {great}, which "The Great American Songbook" contains in
  full). 0.6 accepted every true match and rejected every false one in testing.
- **The owned-album filter is still load-bearing.** "The Lady Sings" (a real MB
  compilation) vs the owned "Lady Sings The Blues" scores 0.80 — text cannot
  separate them. `_assembly_owned_keys` catches it, and did so in the live test.
  The two guards are complementary; keep both.
- Prowlarr's `magnetUrl` is a **proxy redirect** through Prowlarr with the api
  key in the query string, NOT a magnet URI. The bare magnet is in `guid` for
  trackers that publish one; otherwise it is rebuilt from `infoHash`. Results
  with neither (private trackers, ~12 of 158) are dropped — nothing to hand to
  qBittorrent, and Lidarr will not take them.
- A Prowlarr result has no Lidarr guid or indexer id, so `release_grab` can only
  404. `_assembly_grab_for_songs` now skips straight to `add_magnet` when
  `_source == "prowlarr"`.

## Also fixed: 61% of the library was never searched

`wanted_missing()` requested **page 1 only**, silently capping the caller at
1000 records. With 2557 missing albums and the endpoint's fixed
`albums.title ascending` order, `interactive_search_pass` only ever saw titles
from `$` to **"Inside You"**. Everything from J to Z was invisible to it,
deterministically — so those albums were never searched even once.
`Janis Joplin / Pearl` (5/14, monitored, missing for months) had **no entry at
all** in `interactive_search.json`, because the pass never reached it to start
its clock. `wanted_missing()` now paginates; it returns 2557.

No starvation follow-up is needed: `eligible.sort` is by `last_attempt`
ascending and a never-tried album has none (→ 0), so the newly-visible J–Z
albums sort to the FRONT. They become eligible one day after deploy, because
`interactive_search_min_missing_days=1` and their `first_missing` clock starts
on the first pass that sees them.

## What is already built and live

| Piece | State |
|---|---|
| `song_harvest.py` — per-song matching + import | live, `IMPORTING (move)` |
| Gates: title → artist → variant → duration ±10s → **AcoustID** | live |
| One file per track (closest duration) | live |
| `quality` fetched from Lidarr before import | live (see gotcha) |
| Still-wanted files consolidated to `/downloads/_harvest_pending` | live |
| Leftovers deleted + torrent dropped with data | live (`harvest_purge_leftovers=True`) |
| Change gate: folder sig + Lidarr wanted sig | live |
| `add_magnet()` when Lidarr refuses a release | live |
| Lossless +30 / lossy −15 ranking | live |
| Magnet file-list wait 45s → 180s | live |
| LLM per-call logging (subject → answer) | live |
| Lidarr `/manualimport` query timeout 30s → 180s | live |
| **Search-engine → compilation → torrent** | **live** (`prowlarr.py`, `assembly_find_compilation`) |
| **`wanted_missing()` pagination** | **live** (was capped at 1000 of 2557) |

## Gotchas that cost real time — do not rediscover

- **A STOPPED MAGNET NEVER GETS A FILE LIST.** A magnet carries no file names;
  they live in metadata fetched from peers, and a stopped torrent connects to
  nobody. `add_magnet(paused=True)` plus the `qbt.pause()` before the file-list
  wait therefore made every magnet candidate *structurally* unverifiable — the
  wait could only time out, and the release was discarded as "no file list".
  `The Essential Billie Holiday 3 cd boxset[flac]`, holding 27 of that album's
  missing sides, was blocklisted this way. Raising the wait 45s → 180s in an
  earlier session was treating a symptom that had nothing to do with time; it
  only made the stall four times longer. The fix is qBittorrent's own
  `stopCondition=MetadataReceived` (≥4.5): the torrent starts, fetches only
  metadata, and qBittorrent stops it the instant that lands. Measured: 65 files
  known after **5 seconds, 0 bytes of content**. Never pause before you have the
  file list; pause the moment you do.
- **"produced no file list" is now meaningful.** After the above, that message
  means a genuinely dead swarm (0 seeders, so no peers to fetch metadata from),
  not a bug. Those candidates are ranked last by the seeder band.
- **A restart inside the metadata wait orphans the torrent.** Cleanup is purely
  in-process, so killing the container mid-wait leaves a stopped, 0-byte torrent
  that nothing ever reaps — and no warning is logged, because the removal code
  never ran. 13 such orphans were cleared by hand on 2026-08-05. A startup
  reaper (stopped + 0 bytes + no metadata + not in any plan's `tried`/`queue`)
  is still TODO.

- **A ManualImport entry without `quality` silently fails.** Lidarr accepts the
  command, approves the track, then throws
  `NullReferenceException at FileNameBuilder.BuildTrackFileName` and the file
  never lands. Only `lidarr.debug.txt` shows it; `lidarr.txt` says just
  "Couldn't import track", which reads like a permissions problem and is not.
  Fetch quality from `GET /api/v1/manualimport?folder=…` per folder.
- **NEVER use Unraid Docker UI → Edit → Apply.** The image is built locally and
  Apply makes Unraid try to *pull* it, which deletes the tag. Recover by
  retagging the dangling image, then rebuild incrementally. Recreate over SSH
  with `tools/tpl2run.py` instead (it omits two labels — add
  `net.unraid.docker.icon` and `net.unraid.docker.webui` manually).
- **`docker restart` does not move a container to a rebuilt image** — a
  container is pinned to the image id it was created from. Config changes are
  fine with a restart; new code needs remove-and-run.
- Unraid host has **no `python3`** — run python via `docker exec cue_pipeline python`.
- Park work is **bash over SSH**, not PowerShell. Key: `C:\Users\zvani\.ssh\id_ed25519`.
- Gemini is a dead end: that key's project has
  `generate_content_free_tier_requests limit: 0`. The 3090 (`qwen2.5:14b` at
  `192.168.1.32:11434`) is the LLM. `llama3.2:3b` is a safe 3.1 GB fallback
  (11/13 vs 13/13); avoid `gemma3:27b` — slower AND it produced a false positive.
- Windows dev box: run python as `.venv\Scripts\python.exe`; the system python
  has no `mutagen`/`requests`, so importing `orchestrator` fails outright.

## Deploy procedure (proven, use exactly this)

```bash
# 1. edit in C:\ESD\cue_pipeline  (NOT a git repo)
# 2. copy into the ONLY repo that can push:
#    C:\Users\zvani\Documents\GitHub\cue_pipeline\Lidarr_accessory  (branch main)
#    verify the diff is only your change, commit, push
# 3. on PARK:
cd /mnt/user/appdata/cue_pipeline_src && git fetch -q origin && git reset -q --hard origin/main
rm -rf /tmp/cuebuild && mkdir -p /tmp/cuebuild
cp *.py /tmp/cuebuild/ && cp -r tools /tmp/cuebuild/
printf 'FROM cue_pipeline:latest\nRUN find /app -maxdepth 1 -name "*.py" -delete && rm -rf /app/tools\nCOPY *.py /app/\nCOPY tools/ /app/tools/\n' > /tmp/cuebuild/Dockerfile.inc
cd /tmp/cuebuild && DOCKER_BUILDKIT=1 docker build -f Dockerfile.inc -t cue_pipeline:latest .
docker rm -f cue_pipeline && bash /tmp/gen2.cmd
```

A FULL `docker build` STALLS (re-downloads ffmpeg's ~200-package tree) — always
build incrementally as above. `/tmp/gen2.cmd` is the template-generated
`docker run` with the two extra labels; regenerate it with `tools/tpl2run.py` if
it is gone.

**After every recreate, verify:** mounts NON-EMPTY (`/downloads`, `/music`) —
an empty bind mount once caused 85 torrents to be removed — plus
`--user 99:100`, the `dockerman` label, WebUI 200 on 8830, and zero tracebacks.

## Health check

```bash
docker logs cue_pipeline 2>&1 | grep -E 'harvest (pass|purge)|compilation hunt|AcoustID rejected|Traceback' | tail -30
```

Settings live in the WebUI Settings tab (`/config/webui_overrides.json`, HIGHEST
precedence — it beats env/template). All harvest and compilation-hunt knobs are
under "Needs attention & Assembly".

## TODO / next

- **The unverified source-folder deletion is the top open bug.** Confirmed data
  loss: `/downloads/Hans Zimmer/Crimson Tide/Expanded Score` (10 mp3s) went
  `content-identify ... 10/10 (100%)` → `Removed source folder` in **7 seconds**
  with no import in between; Lidarr threw `FileNotFoundException`, the album is
  **0/10**, and no audio survives. A healthy import logs `handoff succeeded` and
  `Post-import check: Lidarr reflects N/N` *before* deleting; 65 of 96
  `Removed source folder` events had neither within 25 lines (heuristic — the log
  is thread-interleaved, so a lead, not a count). Mitigation while it is open:
  `staging.delete_source_folder_on_success = false`. Call sites: orchestrator.py
  ~1048, 1459, 2087, 4353, 4545, 4582 — the one at 4545 is the *sound* one
  (gated on `_wait_for_manual_import`, library-name check advisory by design,
  see the comment at ~4518), so this needs targeted work, not a blanket guard.
- **`song_harvest` counts `imported` on command ACCEPTANCE, not completion**, and
  runs `purge_leftovers` on that same optimistic signal. Verified 14 of 16
  ManualImport commands still `queued` while already logged as imported — so the
  log's "imported" number is a submission count. Not lossy today (the purge
  excludes the imported paths; 81/81 pending files were intact), but
  `qbt.remove(..., delete_files=True)` at song_harvest.py:829 rides the same
  signal. It has never fired only because its match test one line above
  (`nm in src_dir`) is a reversed substring comparison.
- A startup reaper for metadata-orphaned torrents (see the gotcha above).
- The compilation hunt shares `assembly_hunt_per_pass` (2) with the older artist
  hunt. If both are active on many plans, consider a separate budget.
- Coverage is genuinely thin for 1930s–40s material and that is the indexers,
  not the logic — but re-measure now that magnets can actually be verified,
  because every previous "no file list" verdict was meaningless.
