# cue_pipeline — handoff for the next session

Written 2026-08-04. Everything below is DEPLOYED and running unless marked TODO.

## The one job left: find-the-compilation workflow

**Goal.** For a track Lidarr is missing that exists only on compilations, work out
*which* compilation carries it, then find that compilation on the indexers.

Today the song hunt searches indexers by **artist**, ranks whatever comes back,
grabs it, and inspects the file list — it discovers the track list only *after*
downloading. The user wants the opposite order:

1. Take a wanted track (title + artist + duration).
2. Search the web to find which compilations / box sets contain that recording.
3. Take those compilation TITLES and search the indexers for them
   (`lidarr.release_search_artist`, or a direct Prowlarr query).
4. Feed the result into the existing rank → grab → harvest path.

**Why it matters here.** `Billie Holiday / The Love Songs` is 16/50, and 29 of
the 34 missing tracks are 1930s–40s sides absent from the library — e.g.
`Gloomy Sunday`, `Ghost of Yesterday`, `Trav'lin' All Alone`, `Carelessly`.
Those only ever appear on compilations, and blind artist-level searching keeps
grabbing the wrong ones.

**Design notes / traps**
- MusicBrainz already answers step 2 without a scraper: a recording's releases
  are queryable, and `musicbrainz.py` exists in this repo. Try that BEFORE any
  web search — it is structured, free and rate-limited politely.
- Only 3 indexers are enabled and 100/100 Billie Holiday candidates came from
  one (The Pirate Bay via Prowlarr), all magnets. Expect thin coverage.
- Lidarr's `POST /api/v1/release` REFUSES any release it cannot attribute to a
  library artist (404 `Unable to find matching artist and albums`) — every
  cross-artist compilation. `qbt.add_magnet()` already exists for exactly this.
- Verify a grab by INFOHASH (`btih_from_magnet`), never by queue title.

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
| Search-engine → compilation → torrent | **TODO — this session's job** |

## Gotchas that cost real time — do not rediscover

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
docker logs cue_pipeline 2>&1 | grep -E 'harvest (pass|purge)|AcoustID rejected|Traceback' | tail -30
```

Settings live in the WebUI Settings tab (`/config/webui_overrides.json`, HIGHEST
precedence — it beats env/template). All harvest knobs are under
"Needs attention & Assembly".
