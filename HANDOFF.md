# cue_pipeline — session handoff

Written 2026-08-08. Everything marked SHIPPED is deployed and running on PARK.
Read `NEXT_SESSION.md` too — it holds the older gotchas that still apply.

---

## 1. Deployment process (proven — use exactly this)

The container runs a **locally built image**. There is no registry. Every code
change needs a rebuild AND a container recreate.

### Where the code lives

| Location | Role |
|---|---|
| `C:\ESD\cue_pipeline` | Working copy. **NOT a git repo.** Edit here. |
| `C:\Users\zvani\Documents\GitHub\cue_pipeline\Lidarr_accessory` | The ONLY repo that can push (branch `main`). |
| `/mnt/user/appdata/cue_pipeline_src` (PARK) | Checkout the build reads from. |
| `/mnt/cache/appdata/cue_pipeline` (PARK) | `/config` — state files, logs, settings. |

### Step 1 — copy into the repo, verify, push

```bash
cd /c/ESD/cue_pipeline
R="/c/Users/zvani/Documents/GitHub/cue_pipeline/Lidarr_accessory"
cp <changed files> "$R/"
cd "$R" && git diff --stat        # confirm ONLY your change
git add -A && git commit -m "..." && git push origin main
```

### Step 2 — build + recreate on PARK

SSH: `ssh -i C:\Users\zvani\.ssh\id_ed25519 root@192.168.1.200` (bash, not
PowerShell).

```bash
cd /mnt/user/appdata/cue_pipeline_src && git fetch -q origin && git reset -q --hard origin/main
rm -rf /tmp/cuebuild && mkdir -p /tmp/cuebuild
cp *.py /tmp/cuebuild/ && cp -r tools /tmp/cuebuild/
printf 'FROM cue_pipeline:latest\nRUN find /app -maxdepth 1 -name "*.py" -delete && rm -rf /app/tools\nCOPY *.py /app/\nCOPY tools/ /app/tools/\n' > /tmp/cuebuild/Dockerfile.inc
cd /tmp/cuebuild && DOCKER_BUILDKIT=1 docker build -f Dockerfile.inc -t cue_pipeline:latest .
docker rm -f cue_pipeline && bash /tmp/gen2.cmd
```

**A full `docker build` STALLS** (re-downloads ffmpeg's ~200-package tree).
Always build incrementally from `cue_pipeline:latest` as above.

**`docker restart` does NOT pick up new code** — a container is pinned to the
image id it was created from. Config-only changes are fine with a restart; code
needs remove-and-run.

**NEVER use the Unraid Docker UI → Edit → Apply.** Apply makes Unraid try to
*pull* the locally-built image, which deletes the tag. Recover by retagging the
dangling image. `/tmp/gen2.cmd` is the template-generated `docker run` plus two
labels the template omits (`net.unraid.docker.icon`, `net.unraid.docker.webui`);
regenerate with `tools/tpl2run.py` if it goes missing.

### Step 3 — verify EVERY time

```bash
docker ps --filter name=cue_pipeline --format "{{.Status}}"
for m in /downloads /music /config; do docker exec cue_pipeline sh -c "ls $m | wc -l"; done  # must be NON-EMPTY
docker inspect cue_pipeline --format 'user={{.Config.User}} mgr={{index .Config.Labels "net.unraid.docker.managed"}}'
curl -s -o /dev/null -w "%{http_code}\n" http://192.168.1.200:8830/     # expect 200
docker logs cue_pipeline 2>&1 | grep -c Traceback                       # expect 0
```

An empty bind mount once caused **85 torrents to be removed**. Check the mounts.

**The WebUI takes ~30-60s to bind.** Polling at 15s gives a false failure — I
mis-declared a healthy container broken twice this way. Loop until 200.

### Notes

- Unraid host has **no `python3`** and **no `curl` inside the container**. Run
  python via `docker exec cue_pipeline python`, and use `urllib` not curl inside.
- Settings live in `/config/webui_overrides.json` (**highest precedence — beats
  env and config.yaml**). It is nested by section: `{"lidarr": {...},
  "qbittorrent": {...}}`. Writing a key at the top level does nothing.
- A new tunable needs **THREE** edits or it silently uses the dataclass default
  while the UI shows it saved — see `cue-pipeline-config-wiring-trap` memory.

---

## 2. Shipped this session

Commits `ec54ca0` … `7f7466c` on `main`.

### Find-the-compilation (the original task) — SHIPPED

MusicBrainz answers which compilations carry an album's missing songs, ranked by
how many each holds; those titles are searched by name via **Prowlarr** (Lidarr's
`/api/v1/release` takes only an albumId/artistId — it cannot be given free text);
results feed the existing rank → grab → verify → harvest path.

- `prowlarr.py` (new) — free-text search, pinned to the indexers **Lidarr** has
  enabled (`indexer_ids_from_lidarr`), because the wider Prowlarr set is noise.
- `musicbrainz.py` — `compilations_for_tracks()`, `recording_releases()`.
- `assembly_find_compilation()` + "Find compilation" button + 10 settings.
- Verified live: *Master Collection contains 4 needed song(s) — kept.*

### Torrent / grab fixes — SHIPPED

- **A stopped magnet never fetches metadata.** Added stopped + paused again, then
  waited 180s for a file list that could never arrive, and blocklisted the
  release. Now uses `stopCondition=MetadataReceived` (65 files in 5s, 0 bytes).
- **Dead-grab reaper missed `forcedDL`** — the pipeline's own force-start moves
  torrents out of the states the reaper recognised. Grace raised to 24h because
  seeder counts are momentary (85 "dead" torrents → only 38 still dead 7h later).
- **Category boundary enforced.** Several paths enumerated EVERY torrent and
  deleted with data. `qbt_category` + `_qbt_ours()`, fail-closed.
- **Self-added torrents tagged** `cue-assembly` so the deselect pass never starts
  a hunt's grab before it is narrowed.

### Import / library fixes — SHIPPED

- **`wanted_missing()` fetched page 1 only** — 1000 of 2557, so everything
  sorting after "Inside You" (incl. all Cyrillic/Arabic titles) was **never
  searched once**. Now paginates.
- **Same-titled albums** — Weezer has SEVEN "Weezer". `find_album` now takes
  `track_count` + `year`.
- **Multi-CD library folders** — `absoluteTrackNumber` comes back EMPTY from
  `/api/v1/track?albumId=&albumReleaseId=`, so the fallback `trackNumber`
  (restarts per disc) collided and **two of three CDs were silently dropped**
  (16 of 45 mapped). Position is now derived by ordering on (medium, track);
  three indexes tried: `(disc,track)` → ordered position → normalized title.
- **Library audit repairs instead of giving up** — imports by tag track number,
  which beats Lidarr's `Has missing tracks` rejection (that is a verdict about
  the candidate list, not an explicit trackId import) and its
  `Couldn't find similar album` (folder named after the edition).
- **Audit always walks in full on the first pass after a restart** — the
  signature gate skipped the ENTIRE audit, hiding discrepancies forever.
- **Assembly plans drop matches whose source file is gone** — the harvest imports
  in MOVE mode, so plans read "14/14 assembled" with every source deleted.
- **LLM warmup moved to a background thread** — it ran inline and, with the GPU
  busy on another model, hung the whole pipeline (no WebUI, no passes).

### Incremental state — SHIPPED

Per the standing rule: `deselect_planned.json` (new, checkpointed every 10
torrents), sweep ledger flushed per verdict.

**Hard-won nuance:** incremental means *record each verdict as it is decided*,
never *record an item when you first see it*. I broke this three times in one
day — marking at discovery stranded 11 Weezer folders; end-of-pass marking wrote
nothing because passes rarely finish; persisting the **relative** "redundant
edition" verdict hid 123 folders and the sweep handed off 0 for pass after pass.

---

## 3. OPEN — start here

### 3a. Lidarr API rate limit — REQUESTED, NOT IMPLEMENTED

The user asked for a configurable minimum interval between Lidarr calls.
**Measured 2026-08-08** (do not re-measure, just build it):

```
Lidarr log lines/min : 226, 257, 101, 225, 229, 163
cue_pipeline lines/min: 78, 51, 48, 85, 62, 45
Endpoint mix: /album 62, /artist 24, /command 10, /manualimport 9, /wanted 3, /queue 3
```

**Recommended implementation** — one choke point, no missed call sites:
`LidarrClient` builds a `requests.Session`; subclass it (or wrap
`session.request`) with a process-wide lock + `min_interval` sleep, so `_get`,
`_post`, `_put` AND the direct `self.session.get(...)` calls in
`release_search` / `release_search_artist` are all covered. Add
`lidarr_min_request_interval_ms` (default 0 = off) wired in all three places.

Note the biggest win is probably **caching `/api/v1/album` and `/api/v1/artist`**
per pass rather than throttling — 86 of ~110 sampled calls were those two, and
the audit re-fetches the same album repeatedly.

### 3b. Unverified source-folder deletion — CONFIRMED DATA LOSS, still open

`/downloads/Hans Zimmer/Crimson Tide/Expanded Score` went
`content-identify … 10/10 (100%)` → `Removed source folder` in **7 seconds**
with no import in between. Lidarr threw `FileNotFoundException`, the album is
**0/10**, and no audio survives — only `.xml` sidecars.

A healthy import logs `handoff succeeded` and `Post-import check: Lidarr reflects
N/N` **before** deleting. 65 of 96 `Removed source folder` events had neither
within 25 lines (heuristic — thread-interleaved log, so a lead not a count).

**Mitigation while open:** `staging.delete_source_folder_on_success = false`.
Call sites: `orchestrator.py` ~1048, 1459, 2087, 4353, 4545, 4582. The one at
4545 is the *sound* one (gated on `_wait_for_manual_import`); this needs targeted
work, not a blanket guard.

### 3c. Verify the LOTR / audit work actually lands

Deployed but **not yet observed completing**. Expected:

| Album | Now | Expected |
|---|---|---|
| Fellowship (45808) | 37/74 | switch 74 → 37-track, re-import → **37/37** |
| Two Towers (45807) | 16/45 | no switch (already 45-track) → **45/45** |
| Gloria Estefan / Gloria! (45038) | 0/16 | **15/16** (track 5 genuinely absent) |

`RefreshArtist` **resets the monitored release** — watched Fellowship go 0/37 →
0/74 after a refresh. Any release we select is transient unless the import runs
immediately after, which is why `_align_release_to_disk` is now called from the
audit repair path with `allow_when_populated=True` (exact count match only).

### 3d. Smaller open items

- **0 post-import confirmations** across ~42 handoffs — files are handed to
  Lidarr and there is no evidence they land. Same question the session opened
  with; still unanswered.
- **68,403 `.xml` sidecars** in the library (~1 per track). Not cue_pipeline's;
  worth identifying which custom script writes them.
- `HarvestLedger.save()` and `_save_isearch_state()` are still end-of-pass only.
- Deselect pass takes ~1h for ~244 torrents; sweep ~2min for ~800 folders.

---

## 4. Environment quick reference

| Thing | Value |
|---|---|
| PARK | `192.168.1.200`, key `C:\Users\zvani\.ssh\id_ed25519`, **bash over SSH** |
| Lidarr | `http://192.168.1.200:8686` key `7488c03875234ab2b42a6c88ecee5553` |
| Prowlarr | `http://192.168.1.200:9696` key `bb2822e6ba00457bb6d6582b14260c56` |
| qBittorrent | `http://192.168.1.200:8080`, category **`lidarr`** (never touch others) |
| Ollama | `http://192.168.1.32:11434` = **the Windows dev box itself**, `qwen2.5:14b` |
| WebUI | `http://192.168.1.200:8830` |
| Windows python | use `.venv\Scripts\python.exe` — system python lacks mutagen/requests |

**Ollama gotcha:** a 404 from `/api/generate` means the MODEL is missing, not the
endpoint. The store was found empty on 2026-08-06 (`ollama pull qwen2.5:14b`).
The GPU is shared with the user's own work — do not assume it is free.
