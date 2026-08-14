# cue_pipeline — handoff

Written 2026-08-14, end of a long session. Everything marked SHIPPED is
deployed and running on PARK. `README.md` describes what the pipeline does;
this file is what a *fresh session* needs: how to deploy, what changed today,
what is still open, and the mistakes worth not repeating.

Read `NEXT_SESSION.md` too — the older gotchas in it still apply.

---

## 1. Deployment (use exactly this)

The container runs a **locally built image**. No registry. Every code change
needs a rebuild AND a recreate — `docker restart` does not pick up new code.

| Location | Role |
|---|---|
| `C:\ESD\cue_pipeline` | Working copy. **NOT a git repo.** Edit here. |
| `C:\Users\zvani\Documents\GitHub\cue_pipeline\Lidarr_accessory` | The only repo that can push (`main`). |
| `/mnt/user/appdata/cue_pipeline_src` (PARK) | Checkout the build reads. |
| `/mnt/cache/appdata/cue_pipeline` (PARK) | `/config` — state, logs, settings. |

```bash
# 1. copy in, verify, push
cd /c/ESD/cue_pipeline
R="/c/Users/zvani/Documents/GitHub/cue_pipeline/Lidarr_accessory"
cp <changed files> "$R/" && cd "$R" && git diff --stat   # confirm ONLY your change
git add -A && git commit -m "..." && git push origin main

# 2. build + recreate on PARK  (ssh -i C:\Users\zvani\.ssh\id_ed25519 root@192.168.1.200, bash)
cd /mnt/user/appdata/cue_pipeline_src && git fetch -q origin && git reset -q --hard origin/main
rm -rf /tmp/cuebuild && mkdir -p /tmp/cuebuild
cp *.py /tmp/cuebuild/ && cp -r tools /tmp/cuebuild/
printf 'FROM cue_pipeline:latest\nRUN find /app -maxdepth 1 -name "*.py" -delete && rm -rf /app/tools\nCOPY *.py /app/\nCOPY tools/ /app/tools/\n' > /tmp/cuebuild/Dockerfile.inc
cd /tmp/cuebuild && DOCKER_BUILDKIT=1 docker build -f Dockerfile.inc -t cue_pipeline:latest .
docker rm -f cue_pipeline && bash /tmp/gen2.cmd
```

**Traps, all of which have bitten:**

- A **full** `docker build` stalls (re-downloads ffmpeg's ~200-package tree).
  Always build incrementally from `cue_pipeline:latest`.
- **Never** use Unraid Docker UI → Edit → Apply: it tries to *pull* the local
  tag and deletes it. `/tmp/gen2.cmd` is the known-good `docker run` (6 mounts,
  `--user 99:100`, 3 labels). Regenerate with `tools/tpl2run.py` if lost.
- The WebUI needs 30–60 s to bind. Poll until 200 before calling it broken.
- **Verify every time:** mounts non-empty (an empty `/downloads` once removed 85
  torrents), `--user 99:100`, dockerman label, WebUI 200, `grep -c Traceback` = 0.
- A new setting needs **three** edits — dataclass field, settings-registry tuple
  *and* group entry, `OrchestratorConfig(...)` in `main.py`. Miss the third and
  the UI shows the saved value while the pipeline runs the default, silently.
- `webui.py` holds the UI as a **raw** `r"""..."""` string. `py_compile` says
  nothing about the JS inside. Extract the `<script>` blocks and `node --check`
  them, every time.
- Verifying against the LIVE library is not optional — see §4.

---

## 2. Shipped today (28 commits)

### Identification
- **Song-title album resolution.** When no name test can work, match the
  folder's songs against each incomplete album's track list (≥60% + clear
  margin). This is what finally imported the Cyrillic-titled albums.
- **MusicBrainz aliases** in `find_artist` as a last resort — `Lauryn Hill` →
  `Ms. Lauryn Hill`. Exact hit on name/sort-name/alias only; cached including
  misses (MB allows ~1 req/s).
- **`_norm_artist` fixed twice**: it ran `[^a-z0-9]` over un-folded text, so
  `Лили Иванова` normalised to the **empty string** — every Cyrillic artist
  collided. Now transliterates first; non-Latin names that still fold to nothing
  fall back to codepoints. 733 artists → 733 distinct keys (was 732/1).
- **`_demojibake`** — cp1251-read-as-latin1 tags (`Ëèëè Èâàíîâà`). Strict test:
  ≥3 high-range chars AND ≥40% of letters, so `Beyoncé` is untouched.
- **Honorifics** — leading Ms/Mrs/Miss/Mr folded. Deliberately not St./Dr./DJ.
- **Same-titled albums told apart by year.** Weezer has seven albums called
  *Weezer*; the complete Blue Album was answering for the missing Green one.
- **Filenames parsed properly** — `{Artist} - {Album} - NN - {Title}` (Lidarr's
  own scheme) previously yielded the whole 70-char stem as the "song title".

### Audit / import
- **Under-registered albums** are now a discrepancy. The audit only ever flagged
  *zero-file* albums, and two guards bailed on anything with one file — so
  16/24 with all 24 files present was skipped three times over. **121 albums /
  ~869 tracks** library-wide; 70 repaired in the first pass.
- **Quality probed per medium folder**, not just `audios[0].parent` — this alone
  pinned TRON: Ares at exactly 16 of 24.
- **Container folders expanded** (`Артист/Студийные альбомы/Альбом`), and
  **medium folders kept whole** (`12 Vinyl 01`, `Enhanced CD 02`, `Hybrid SACD
  (SACD layer, 2 channels) 02`).
- **RenameFiles after an in-place import**, so a rescued album follows
  `/config/naming` instead of staying at its old path.
- **Cue ledger** (`cue_seen.json`) — a verdict per `.cue`, so a restart never
  re-splits. One image had been decoded seven times in two days.
- **Combined discs skipped** when every album on them is already complete.
- **SACD ISOs** no longer extracted for an album already owned.
- **Cue queue de-duplicated** — three code paths enqueued, one checked; the
  queue held 48 entries for 19 cues.

### Search / torrents
- **Cost ceiling** `interactive_search_max_gb_per_album` (2.5). Nothing ever
  looked at what a release *cost*: 27 GB of 2Pac for one album, 16 GB of Bon
  Jovi, 8 GB of Little Feat.
- **Placeholder artists skipped** (Various Artists/VA/Soundtrack/Unknown) — a
  49-disc Genshin OST was grabbed against Various Artists.
- **Video and non-audio payloads refused** by title (BDRip/720p/x264; 3D models,
  ebooks, software). DVD-Audio / Blu-ray Audio / SACD exempt.
- **Title containment** — tracker decoration no longer buries a good release
  (`jewel 0304` was 0.26 against a 0.45 floor; now 1.00). Guarded so dotted
  initialisms can't match letter soup (`M.I.A.` vs `A.R.M.I.A`).
- **Metadata read before pausing** a magnet — a paused magnet never fetches it,
  so *every* magnet grab had been "accepted, unverified".
- **Forced attribution on grab** (`albumId`/`artistId`) — the UI's "Grab
  Release" confirm. Bare POST 404s; with ids it's 200.
- **Deselect resolves the artist by album title** when the folder isn't one
  (`BJ_Discography` → Bon Jovi). 31 albums → 22 deselected.

### Converter
- Skip already converted; destination **root folder** (picker + **+** to
  create); **copy lossy instead of skipping**; **Convert / Cancel / Clear /
  Pause**; queue **survives a restart** (`convert_queue.json`).
- **Yields to the pipeline** — holds its queue while a split/extraction runs,
  and runs `ionice -c 3 nice -n 15`. `CONVERT_NICE` overrides.
- **Status payload bounded** — it shipped every queued job every 3 s; a
  whole-library run is ~86,500 (8.5 MB → 3.7 KB). Queue is grouped **by album**.
- Total progress bar measures the **batch**, not an average over the queue.

### Other
- **Prefer lossless over lossy** — quarantines the lossy twin to
  `<album>/_superseded_by_lossless/` and rescans. **60 albums / 604 files.**
- **Assembly counts tracks already in the library** — it had no "present" state,
  so owned songs were listed as missing.
- **Clear log** button (truncates through the live handler, removes rotations).

---

## 3. Open items

1. **Frida — Shine (1984) is GONE.** 12 FLACs + 12 MP3s destroyed by me (see
   §4). Not recoverable on this box; the user was re-downloading it. Verify it
   came back.
2. **Lidarr's recycle bin is OFF** (`recycleBin: ''`, `recycleBinCleanupDays:
   0`). Turning it on would have made that loss recoverable. Worth proposing.
3. **`interactive_search_max_candidates` is 1000.** One bad artist match becomes
   1000 grab attempts. Suggest 5–10. Not changed — it's the user's setting.
4. **318 albums still partially imported.** The audit is working through them;
   watch `under-registered` lines. Not all are fixable — many are genuinely
   missing tracks or oversized monitored releases.
5. **Torrents left over from before the guards**, still downloading: Little Feat
   discography (7.8 GB, 88%), Barry Manilow, Natalie Cole. New grabs are
   guarded; these predate it.
6. **Orphan uncategorised torrents** in qBittorrent (two Simply Red concert
   videos, a Judge Judy episode, a Hans Zimmer WavPack). Lidarr sent the Simply
   Red ones — "Report sent to qBittorrent" — but they carry no category and have
   **zero** history rows, which I could not explain. Worth chasing: uncategorised
   torrents escape every category-scoped guard.
7. **`.mkv` files sitting in the library** (`Bon Jovi/Jon Bon Jovi - Bonus
   N-video.mkv`). Video in the music library.
8. **Census watcher running on PARK** — `/tmp/census_watch.log`, re-censuses
   `/music/Music` every 10 min for 4 h and reports only losses. Baseline
   `/config/.file_census.json`: **7,567 albums / 91,203 files**. Re-run manually:
   `docker exec cue_pipeline python3 /tmp/snap.py`.
9. **Prefer-lossless has run 24 times** but the 604-file backlog is not
   confirmed cleared. Check `_superseded_by_lossless` folders.
10. **Cloud LLM** (`cloud_llm.py`, gemini-2.0-flash) is wired but unused. The
    LLM now does only three things: `parse_artist_album`, `repair_cue`,
    `confirm_album_match`. Suggested (not built): per-call fallback to the API
    for `repair_cue` / `confirm_album_match` when the shared GPU is busy.

---

## 4. Mistakes worth not repeating

**I destroyed an album.** Building "prefer lossless over lossy", I called
ManualImport with `importMode="move"` + `replaceExistingFiles=True` on files
**already inside the library**. Lidarr moved each file onto itself and deleted
the "existing" copy; with the recycle bin off that was permanent. Frida/Shine —
24 files — gone. I ran it against the live library as the *first* test of a
feature whose whole purpose is removing files.

Consequences now baked in: `replace_existing` is **removed from
`LidarrClient`** so no caller can reach that combination; the feature moves
files to a quarantine folder and never deletes; it was re-tested on scratch
files, verifying the file count before equals after.

**Other things to carry forward:**

- Test destructive changes on **copies**. Every rule I invented and checked
  against the live 733-artist / 9,049-album data caught a flaw the unit test
  didn't — the honorific fold, the size guard, the title containment.
- Prove a change doesn't regress: diff old vs new verdicts across the whole
  library and *show the changed rows*. That is how the same-title fix was
  justified (50 changes, all toward the safe direction).
- Deploying **wipes the conversion queue** (fixed now — but check
  `convert_queue.json` exists before assuming).
- Don't guess at UI intent. Several rounds were spent rebuilding the Converter
  layout because I inferred instead of looking at the screenshot or asking.

---

## 5. Environment

| Thing | Value |
|---|---|
| PARK | `192.168.1.200`, key `C:\Users\zvani\.ssh\id_ed25519`, **bash over SSH** |
| Lidarr | `http://192.168.1.200:8686` key `7488c03875234ab2b42a6c88ecee5553` |
| Prowlarr | `http://192.168.1.200:9696` key `bb2822e6ba00457bb6d6582b14260c56` |
| qBittorrent | `http://192.168.1.200:8080`, category **`lidarr`** — never touch others |
| Ollama | `http://192.168.1.32:11434` = the Windows dev box, `qwen2.5:14b`. GPU is **shared with the user's own work** |
| WebUI | `http://192.168.1.200:8830` |
| Windows python | `.venv\Scripts\python.exe`; set `PYTHONIOENCODING=utf-8` or non-Latin names crash the console |

Lidarr naming: `{Album Title} ({Release Year})/{Artist Name} - {Album Title} -
{track:00} - {Track Title}`, artist folder `{Artist Name}`.

**Standing instructions from the user:** fix things *in the pipeline*, never by
hand — they will not open a session every time an import fails. Keep answers
short. Always test your work.
