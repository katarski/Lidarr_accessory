# cue_pipeline — handoff

Consolidated 30 Sep 2026. This file is rewritten, not appended to: everything
below is current. `README.md` explains what the pipeline does; this is what a
fresh session needs — how to deploy, what is true now, what is still open, and
the mistakes worth not repeating.

---

## 1. Deployment (use exactly this)

The image is **built locally on PARK and exists in no registry**. Every code
change needs a rebuild AND a container replace — `docker restart` keeps the old
image.

**The routine used for every fix since 29 Sep** (the Windows repo pushes;
PARK only pulls):

```bash
# Windows, in the repo: commit, push, ship the tree to PARK
git push origin main
tar --exclude=.git --exclude=__pycache__ -cf - . | ssh root@192.168.1.200   'rm -rf /tmp/cue_test && mkdir -p /tmp/cue_test && tar -xf - -C /tmp/cue_test'
# PARK: tests in the image, sync the checkout, deploy
docker run --rm -v /tmp/cue_test:/src -w /src -e PYTHONIOENCODING=utf-8   --entrypoint python cue_pipeline:pre-gate-20260929 -m unittest discover -s tests -t .
D=/mnt/cache/appdata/cue_pipeline_src
git -c safe.directory=$D -C $D fetch -q origin && git -c safe.directory=$D -C $D reset -q --hard origin/main
sh /tmp/cue_test/tools/deploy.sh guard-<date>-<what>
```

`tools/deploy.sh` builds FROM `cue_pipeline:pre-gate-20260929` (the last full
build) with the repo's `*.py` and `tools/`, checks `OrchestratorConfig` is
still a dataclass, generates the run command from the flash template
(`tools/tpl2run.py`), and `tools/mkrun.py` refuses unless it has exactly 6
mounts, 52 env vars, HA_URL/HA_TOKEN/LLM_* present and the image last. Only
then is the container replaced. Live now: **`guard-20260930-cpucap`**
(9a88678). Earlier tags for rollback: `guard-20260930-floatrange`,
`guard-20260930-harvestgate`,
`guard-20260930-cuecodec`,
`guard-20260930-posexact`,
`guard-20260930-onepath`,
`guard-20260930-envbool`,
`guard-20260930-setsave`,
`guard-20260930-auditstate`,
`guard-20260930-csrf`,
`guard-20260930-cli03`,
`guard-20260930-isstate`,
`guard-20260930-auditidx`,
`guard-20260930-reconcile`,
`guard-20260930-isearch`,
`guard-20260930-compneg`,
`guard-20260930-lossless`,
`guard-20260930-disccues`,
`guard-20260930-multidisc`,
`guard-20260930-titles`, `guard-20260929-songs`, `-asmplan`,
`-f4`, `-claims`, `-assembly`, `-binding`, `-deleters`, `-pairing`,
`-names`, `pre-gate-20260929`.

The bundle procedure below is only for commits made ON PARK.

| Location | Role |
|---|---|
| `C:\Users\zvani\Documents\GitHub\cue_pipeline\Lidarr_accessory` | the repo that can push (`main`) |
| `/mnt/cache/appdata/cue_pipeline_src` (PARK) | the checkout the build reads |
| `/mnt/cache/appdata/cue_pipeline` (PARK) | `/config` — state, logs, settings |

**PARK cannot push.** No `~/.git-credentials`, no helper; it reads the remote
anonymously and push fails. Commit on PARK, then move the commits with a bundle:

```bash
# on PARK (git needs safe.directory: the checkout is root-owned)
D=/mnt/cache/appdata/cue_pipeline_src
G="git -c safe.directory=$D -C $D -c user.name=park -c user.email=park"
$G add <files> && $G commit -F /tmp/msg.txt        # -F a FILE: an apostrophe in
$G bundle create /tmp/cue.bundle main              #    -m breaks ssh quoting
# on Windows
ssh root@192.168.1.200 'cat /tmp/cue.bundle' > cue.bundle
cd "$REPO" && git fetch ../cue.bundle main:refs/remotes/park/x \
  && git merge --ff-only park/x && git push origin main
# back on PARK
$G fetch -q origin && $G reset -q --hard origin/main
```

`origin/master` is an abandoned branch (PARK was once 263 commits ahead of it).
**`main` is the live one.**

Build incrementally — a full build stalls re-fetching ffmpeg's dependency tree:

```bash
rm -rf /tmp/cuebuild && mkdir -p /tmp/cuebuild && cp $D/<changed>.py /tmp/cuebuild/
printf 'FROM cue_pipeline:latest\nCOPY <changed>.py /app/\n' > /tmp/cuebuild/Dockerfile
docker build -q -t cue_pipeline:latest /tmp/cuebuild
docker tag cue_pipeline:latest cue_pipeline:guard-$(date +%Y%m%d)   # see below
```

**Never use the Unraid UI → Edit → Apply for this container** unless a second
tag exists: Apply tries to *pull* `cue_pipeline:latest`, fails, and can drop the
tag. Protection in place:

- **two tags on every build** (`:latest` + a `guard-<date>`), so Apply cannot
  orphan the image;
- **an archive per build** in `/mnt/user/System Backups/docker-images/` via
  `docker-image-backup.sh` (verify with `zstd -t`; its log line reports a bogus
  "1.0K" size because `du` on shfs answers before the flush);
- **`/mnt/user/appdata/scripts/cue_pipeline-restore-image.sh`** — re-tags from a
  guard tag, else the newest dangling image, else loads the archive. Never
  starts the container.

To replace the container **without starting it**, generate the run command from
the flash template and turn it into a `create`:

```bash
docker run --rm --entrypoint python3 -v /boot/config/plugins/dockerMan/templates-user:/tpl:ro \
  cue_pipeline:latest /app/tools/tpl2run.py /tpl/my-cue_pipeline.xml > /tmp/cue.run.sh
# docker run -d  ->  docker create ; --restart no ; re-add the two labels
# tpl2run omits (net.unraid.docker.icon and .webui) or the WebUI link disappears
```

Gate the `docker rm` on assertions about the generated text (6 mounts, 48 env
vars, the image on the last line) and refuse if any `docker run` survived.
**Build that transformation in Python, not sed** — a `\n` in a sed replacement,
and backslashes through ssh→docker→`python -c`, both silently produced a literal
`\n` that would have handed docker `n` as the image name.

Apply also **starts** the container (there is no inert mode: `main.py` takes only
`--config`) and injects `HOST_OS`/`HOST_HOSTNAME`/`HOST_CONTAINERNAME`, so a
hand-created container shows 54 env vars where Apply gives 57.

---

### Tests

`tests/` is plain `unittest`; run it **in the image** (mutagen, ffmpeg):

```bash
# Windows: ship the working tree
tar -cf - --exclude=__pycache__ --exclude=.git . | ssh root@192.168.1.200 'rm -rf /tmp/cue_test && mkdir /tmp/cue_test && tar -xmf - -C /tmp/cue_test'
# PARK
docker run --rm -v /tmp/cue_test:/src -w /src --entrypoint python cue_pipeline:latest -m unittest discover -s tests -t .
```

Green tests are not a deploy check: `tests/test_startup.py` exists because a
class inserted above `OrchestratorConfig` took its `@dataclass` and the
container crash-looped while every other test passed. And never build from a
context copied under `umask 077` — the files become root-only and the
container (uid 99) cannot read `/app/main.py`.

---

## 2. Configuration: three layers, and the last one wins

`main.py` builds the config in this order (~line 1127):

```
load_config(config.yaml)  ->  apply_env_overrides()  ->  apply_webui_overrides()
                              a template env var           webui_overrides.json
                              BEATS config.yaml            BEATS both
```

`put()` ignores an env var that is unset or empty, so an empty template field
means "use the YAML".

**Editing `config.yaml` alone is often a no-op.** Both of these were live traps:

- `flac_compression_level: 5` in the YAML did nothing — `webui_overrides.json`
  held `{"ffmpeg": {"flac_compression_level": 8}}`.
- `ollama.base_url` in the YAML was dead text — the template's `LLM_BASE_URL`
  overrode it, pointing at an address that no longer answered.
- The **AcoustID key in `config.yaml` is the expired public TEST key and is not
  what runs.** `ACOUSTID_KEY` in the template is a different, valid key. Do not
  conclude the key is broken by reading the YAML.

- A **stale `ollama.model` in `webui_overrides.json`** (the 14B, from an old
  "switch models via overrides" trick) beat the template's `LLM_MODEL` for
  weeks: the LLM asked for a model that was gone. Removed 29 Sep. `main.py`
  now WARNS at startup whenever a Settings-tab value overrides a container
  variable — read that line after every deploy.

**Always read the EFFECTIVE value**, without starting the pipeline:

```bash
docker inspect cue_pipeline --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/cue.env
docker run --rm --env-file /tmp/cue.env -v /mnt/cache/appdata/cue_pipeline:/config:ro \
  --entrypoint python cue_pipeline:latest -c 'import sys;sys.path.insert(0,"/app");import main;...'
# load_config -> apply_env_overrides -> apply_webui_overrides, then print the field
```

**Never store a LAN address.** Daniel has moved between .32 and .45 and "it can
be anything". PARK and its containers resolve `daniel` via the router, so every
layer says `http://daniel:11434`.

---

## 3. What protects the library

- **A source folder is never deleted while its audio is still in it.** A real
  import MOVES the files out, so audio still present means nothing was imported.
  `_delete_source_folder` refuses unless the caller supplies proof or
  `_verify_library_reflects_album` confirms Lidarr holds the album; with no
  evidence at all it refuses. Every automated caller funnels through it.
  The two WebUI actions (resolve/discard) bypass it on purpose — a human
  decided. This came from `Hans Zimmer/Crimson Tide`: 10 mp3s deleted seven
  seconds after "content-identify 10/10", album left at 0/10.
  A hand-off is judged by the files it SENT (`_submitted_paths`) and its
  leftovers by a walk of the whole folder, never one level: a multi-disc
  album is handed off at its parent, and a one-level listing read "cleared"
  with Disc 2 still in CD2 (orch2 F9). `_finish_handoff_source` keeps the
  folder and its .cue while any audio Lidarr did not take is under it
  (logs "Keeping ... and its .cue"). Folder deletion is OFF live
  (`delete_source_folder_on_success: false` in the Settings overrides), so
  a successful hand-off removes only the orphan .cue.
- **A multi-disc album is ONE unit of work** (`_multidisc_cue_unit`): a .cue
  in CD1/Disc 2/... of a root with two or more such folders belongs to the
  root. Its job holds the root; the album hand-off's verdict goes to every
  disc .cue in the cue ledger (own signature each, one shared attempt
  count, the root as a fifth field) and to `_skip_seen`; the held entry is
  one, at the root; after a clean import every disc .cue goes, and folder
  deletion targets the handed-off folder (`_source_sentinel`), never the
  .cue's own folder. Before this, each disc .cue handed the whole root off
  again (Nat King Cole 4-CD: 12 hand-offs in an hour) and the emptied disc
  folders were recorded "No companion audio" failures (orch2 F3, loops F6).
  A single-image .cue in a disc folder still splits on its own; CD folders
  loose in the watch root are never one album. The 4 per-disc held entries
  of Nat King Cole "100 Hits" predate this (it gave up in the old code).
- **Prefer-lossless works per folder** (`_prefer_lossless_in_album`): a song
  is (folder, name key), the lossy twin is quarantined in its OWN folder's
  `_superseded_by_lossless/`, and every folder it changed is rescanned
  (orch2 F11). Nothing is deleted.
- **"Could not ask" is never stored as an answer** in the compilation hunt
  or the external audit (orch3 COMP-NEG-1): `compilations_for_tracks`
  returns (results, complete); a partial walk serves one pass and gets no
  cache stamp; a Lidarr failure (generation check) leaves the hunt as it
  was; the audit records nothing for an artist whose album list failed.
- **A Lidarr failure during the interactive search is not an attempt**
  (orch3 ISEARCH-AVAIL-1): a failed search or grab (generation check) or
  `available()` false before a grab ends the album's candidate loop with
  None -- no candidate burned, no Prowlarr fallback, no cooldown stamp --
  and the pass stops there; a failed queue read skips the pass.
- **Reconcile caches "nothing importable" only when Lidarr answered**
  (orch2 F6): the probe snapshots `_lidarr_generation()`; a changed
  generation caches nothing, and with the breaker open the pass stops.
- **The library audit treats a failed album list as unknown** (orch2 F7):
  `_album_index` returns None and caches nothing, and that artist is
  skipped for the pass; a song-title resolve that lost an album to a failed
  read returns None; `_bail_if_green` also gates an album resolved by songs
  or tags (green = holds as many files as the folder, or complete). The
  external audit's Lidarr side (orch2 "missed") was closed by COMP-NEG-1.
- **Interactive-search state is checkpointed per album** (orch3
  ISEARCH-STATE-1): `_save_isearch_state` after every album and artist
  fallback; main.py passes the shutdown event and the pass stops between
  albums. Pruning happens only at the end of a pass.
- **"Could not ask Lidarr" is never an empty answer** (clients CLI-03):
  `wanted_missing` raises `LidarrUnavailable` unless the walk is whole;
  `_monitored_album_status` answers "unknown" (not "skip") when the
  generation moved under it; edition dedup defers a group Lidarr could not
  rank (`_editions_deferred`) instead of recording losers.
- **WebUI POSTs need the page token** (orch3 WEBUI-CSRF-1, `guard-20260930-csrf`). Every
  POST to :8830 must send `X-CUE-Token` (the page has it inline; scripts `GET /api/token`
  first), and one whose Origin/Referer names another site is refused 403 and logged
  "webui: refused POST". The token changes on every restart; an open tab refreshes it itself.
- **Library audit state is `library_audit.state.json`** (orch2 F5, `guard-20260930-auditstate`):
  the first-run marker and the signature of the last COMPLETE pass. A pass with any Lidarr
  failure, unlisted artist, busy folder or failed repair is logged "pass incomplete" and not
  recorded, so the next one walks again. `library_audit.csv` is now replaced each pass
  (rows go to `.csv.partial` first). The root-owned `library_audit.sig`/`.sig.bak` are unused.
- **Settings tab saves only what you change** (orch3 SETTINGS-SAVE-1, `guard-20260930-setsave`).
  A save drops every saved value equal to config.yaml/the container variable, so setting a
  field back to that value hands it to the template again; the tab marks values "saved here;
  else X". Live, all 125 keys are still saved from before (38 equal the template and go at
  the next save). **Owner's call:** 4 saved values beat the template and stay until changed in
  the tab: max albums/pass 300 (template 15), search interval 1000 s (3600), dead-grab grace
  1440 min (360), delete source folder off (template on). Their warnings now reach pipeline.log.
- **CPU caps (30 Sep, owner: "as quiet as possible")**: lidarr `--cpus 1.0 --cpu-shares 256`,
  cue_pipeline `--cpus 0.5 --cpu-shares 128`, both applied live (`docker update`) and in the
  templates' ExtraParams (backups `*.bak-20260930-cpu`); the owner's own pinning is lidarr `2,8`,
  cue_pipeline `1,7`. deploy.sh recreates from the template, so the cap survives deploys.
- **CPU caps are set in the WebUI** (Settings tab, top row; `guard-20260930-cpucap`): the
  current cap and pinned threads of cue_pipeline and Lidarr, and Apply, which changes them live
  through Docker's update call (no restart). Saved in `/config/cpu_caps.json` and applied again
  at every start, since a deploy recreates cue_pipeline with its template's `--cpus`. Only these
  two containers (Lidarr = `$LIDARR_CONTAINER`, else `lidarr`), 0.1..CPU count.
- **Nothing is grabbed for an unmonitored album or artist** — Lidarr will not
  import into one either, so the download is wasted twice. Fails open: no id, or
  a lookup error, and the grab proceeds.
- **prefer-lossless quarantines, never deletes**, and cannot nest: quarantined
  paths are excluded both at the audit and inside the mover. It once nested
  `_superseded_by_lossless` 71 levels deep, which made a Jellyfin validation
  worker spin a core for 13 h.
- `replace_existing` **does not exist** on `LidarrClient` — see §5.
- **"Redundant" is Lidarr's per-file verdict**, never a track count:
  `dedup_downloads.folder_fully_owned` — every file mapped AND rejected "Not an
  upgrade". Used by the pre-split check and the purge. A count deleted a song
  Lidarr did not know, and a FLAC download of an album held as MP3.
- **One redundant album never takes its neighbours**
  (`_dispose_redundant_download`): torrent found by content path in our
  category, queue row by downloadId, a discography leaf only deselected.
- **One worker per folder** (`claims.py`): the CUE job, each hand-off, the
  WebUI Add/Overwrite/Discard, Assembly Add's source unlink and the harvest
  (per source and its purge) claim their folder. `_delete_folder_under_watch`,
  `QbtClient.remove` and `robust_rmtree` refuse a folder another thread holds.
  The sweep is one thread and one pass at a time. Reconcile (per folder) and
  held auto-resolve skip a claimed folder with no verdict and no negative
  cache. In the library, the audit claims each album folder
  (`_claimed_dirs`, skips a held one), and the positional nudge and
  `_manual_move_to_library` wait for that claim, so the audit never sees a
  folder half-written. Left: the audit can still act while Lidarr's own
  ManualImport is moving files in, before the worker's verify claims it;
  the audit's green gate re-polls the album first. Reconcile counts an
  import only when the command completes (`_wait_for_manual_import`); a
  failed or timed-out one waits out the recheck. The log format carries
  `%(threadName)s`.
- **Song evidence decides a grab** (`_verify_torrent`): below
  `verify_track_titles_reject` it is `reject:` (blocklisted); in the gray
  zone up to `verify_track_titles_accept` it is `unsure:` (removed, not
  blocklisted, `_blocklists`); the file count decides only with no title
  evidence. A single disc image is not scored by its one file name. An
  assembly-hunt grab that could not be inspected is removed, not
  blocklisted.
- **One torrent remover** (`Orchestrator._remove_torrent`): qBittorrent must
  answer, our category only, never while claimed; Lidarr's queue rows (by
  downloadId) are dropped with `removeFromClient=false` (blocklisting when
  asked), and we remove the torrent. Lidarr never deletes from the client.
  `QbtClient.lookup` tells "gone" from "could not ask".
- **Nothing concluded during an outage is recorded**: LidarrClient counts
  failures (`failure_generation`, fail-fast breaker); a hand-off in which one
  happened records only facts and is not remembered by the sweep. The
  assembly plan pass prunes no plan in such a pass (nor when a gap album's
  track list is missing), and a keep-set that cannot be read makes the
  deselect and the stalled reaper skip the pass (`_assembly_keep` raises;
  None means only "assembly off").
- **An unanswered LLM question is not a "no"** (`UNAVAILABLE`, `llm_gate.py`):
  the item waits and is re-examined when the gate reopens.
- **Encodes appear only complete** (`_encode_flac`: `.partial`, verify, tag,
  rename); DVD-Audio publishes the whole disc or nothing.
- **Titles compare in any script** (`titlematch.py`) — see README.
- **Deselected is not owned** (`qbt_deselect`): `plan_torrent` gives each
  album `have` (don't download) and `owned` (Lidarr holds it complete, matched
  without the LLM). Only `owned` may ground a delete; the lifecycle pass also
  requires `folder_fully_owned` for every on-disk folder, and never asks the
  LLM. Uncategorised torrents are adopted only if they carry our own tag.
- **A submitted import is not an import**: the harvest purge waits in the
  ledger (`pending`) until every submitted track has a file; unproven audio is
  never deleted; only the torrent whose content IS the folder is removed.
  Assembly Add re-reads the tracks (never imports onto a filled one: an
  explicit-trackId import there is an Upgrade that deletes) and frees a source
  only when its track holds a new trackfile outside staging
  (`LidarrClient.command_succeeded` is the one verdict on a command record).
- **A release title must NAME the album** (`titlematch.album_naming`,
  `_title_relation`): its words a whole field once the artist's credit is
  taken out once, not inside a longer name nor inside a more specific
  album of the same artist (the album list is asked once per search pass;
  a failed ask is not cached). Containing the words is not enough: it
  grabbed "Dusty In Memphis", "Ev'rything's Coming Up Dusty" and "Simply
  Dusty" for "Dusty", and "Blue Eyed Soul" for Simply Red's "Blue". Not
  named scores 0. A colon opens a field but does not close one ("Joker:
  Folie a Deux" is not "Joker"). **Nothing below the title floor is ever
  grabbed**, not even after the relevant candidates were rejected. The
  live floor is **0.45** (`ISEARCH_MIN_TITLE_RATIO` and the Settings tab),
  not the 0.55 default -- test title rules against 0.45.
- **A grab is bound to its own queue row** (`_await_grab`): the infohash, or a
  row new since the pre-grab snapshot whose album id or whole title is the
  release's. Never a substring: Priscilla Ahn's "La La La" once bound to, and
  deleted, Confidence Man's live "5AM (LA LA LA)". A reject removes only the
  row with that hash, and only our torrent.
- **WebUI Add/Overwrite** imports only the files it copied (title pairing onto
  empty tracks), refuses two held files with one name, and deletes the held
  folder only when every song has an equal copy in the library. Discard never
  reaps on an unreadable file list, and deselects by the album's path inside
  the torrent, not a folder-name segment.

---

## 4. Performance: what actually costs, measured on PARK

| operation | cost |
|---|---|
| `/api/v1/manualimport` for one folder | **10–20 s** (Lidarr parses every file first) |
| `RefreshArtist` | **2.5 s**, and everything else queues behind it |
| `mutagen.File(p)` **sniffing** a FLAC | **631 ms** → **264 ms** when the parser is named |
| `fpcalc` fingerprint | ~0.37 s per file |
| FLAC encode, level 8 → 5 | 2.1 s → 1.2 s per track, for 1.35% more size |
| `list_albums_for_artist` | 0.02 s (already cached per artist) |
| the whole on-disk library walk | under a minute |

Everything above is now cached or deduped:

- **`manual_import_candidates`** caches per (folder, artist hint), validated by a
  cheap folder fingerprint (file count + total size + newest mtime) and a
  15-minute TTL; `force=True` bypasses. An import moving files out changes the
  fingerprint, so the cache cannot mask real work. Persisted to
  `/config/manualimport_cache.json`, bounded (4 MB budget, newest first, writes
  debounced to one per 30 s).
- **`refresh_artist`** skips only while the previous command for that artist is
  still queued or running (one cheap GET of `/api/v1/command/{id}`). A blanket
  cooldown would be wrong: every call site is a post-import reconcile.
- **AcoustID** results persist to `/config/acoustid_cache.json`. Only a real
  answer is cached — `_lookup` returns `(status, data)` and an `"error"` is never
  stored, or a spell of bad key/no network would be baked in as "no match"
  forever. Misses expire after 30 days; after three rejected lookups the client
  disables itself for the run.
- **`_extract_embedded_cuesheet`** caches its answer per (path, size, mtime).
  The cueless sweep asks it about every audio file in every pre-split folder on
  a 60 second timer -- measured here, 3,515 files, ~42 ms each: **149 seconds of
  parsing per pass, on a 60 second timer**, re-deriving an answer that cannot
  change unless the file does. Cached, an unchanged pass costs ~1 s. An
  unreadable file is deliberately NOT cached, so a half-written download is
  re-read once it settles. Persisted beside the sweep ledger.
- **`audio_open.File`** is a drop-in for `mutagen.File` that names the parser
  from the extension. `mutagen.File()` with no hint scores every parser it
  knows; py-spy showed four of five worker threads inside
  `mutagen/apev2.py:score` at one instant. Falls back to a full sniff when the
  extension lies. Verified identical on 60 real files (same class, same tags,
  same errors), 2.1× faster.

**Profile before optimising.** Two things I was sure about were wrong: the
library audit's Lidarr lookups were already batched (0.02 s each, 19 s for 758
artists), and a duplicated directory scan I found cost 0.44 s per pass — 0.7% of
one core, not worth a diff.

---

## 5. Mistakes worth not repeating

**An album was destroyed.** Building "prefer lossless over lossy",
ManualImport was called with `importMode="move"` + `replaceExistingFiles=True`
on files *already inside the library*. Lidarr moved each file onto itself and
deleted the "existing" copy; with the recycle bin off that was permanent.
Frida/Shine — 24 files — gone, tested against the live library as the *first*
test of a feature whose purpose is removing files.

- Test destructive changes on **copies**, and prove the file count before equals
  after.
- Prove a change does not regress: diff old vs new verdicts across the whole
  library and show the changed rows.
- **Before caching anything, ask what the cached value means when the underlying
  call FAILED.** A cache that cannot tell "no" from "I could not ask" turns an
  outage into a permanent wrong answer.
- **Unavailable is not "no" — anywhere.** The same bug came back in four
  places (LLM answers, Lidarr lookups, a partial Lidarr index, an in-memory
  skip set that never expired). Each is fixed at the source now; any new
  "could not ask" path must say so distinctly.
- **Do not read a rate off progress lines.** The gap between two
  "[n/758] discrepancy" lines is filled with searches, imports and harvests, not
  scanning — which is how the audit got blamed for 50 minutes of other work.

---

## 6. Open items

1. **10 audit findings are open: `docs/AUDIT_OPEN.md`**, in fix order, each
   with evidence, root cause and the agreed fix (the verifier's correction
   where there was one). Start at item 1 (loops F10). 81 are fixed;
   CLI-04/06/07 were refuted — do not redo them. **The owner compacts
   after every fix: finish one (tests, deploy, verify, these two files),
   then stop.** orch2 F4 (30 Sep) is deployed; its first live
   interactive-search pass was not yet watched -- look for "best title
   match 0.00" lines naming a release that WAS the album.
2. **Orphan CUE whose image is gone** (logged once each, `[ERROR] No companion
   audio next to ...`): `/downloads/Oliver Deriviere - Music From Alone In The
   Dark/` holds only `CDIMAGE.flac.cue` (FILE "CDIMAGE.flac"), `CDIMAGE.log`
   and `scans/`; same for Carly Simon *Have You Seen Me Lately*
   (`edit.flac.cue`). Not investigated: find whether the pipeline imported the
   image and left the cue (then the cue should be retired as done, not an
   ERROR) or the torrent never had it (then it is a real hold).
3. **The owner must re-download** Simply Red *Blue*, the Slim Harpo box and
   Elmore James *The Sky Is Crying* (the old number-only pairing misfiled
   them; the pairing is fixed).
4. **Lidarr's own AlbumSearch found 0 reports** for 9 missing albums (Cyrillic,
   Arabic, Japanese, Latin with curly quotes): indexer coverage, not code.
5. **CUE ledger "gave up" rows** from before 29 Sep may be outage artefacts
   (Lidarr down -> `skipped_unmonitored` x3). Replace/edit the .cue to retry.
6. **Lidarr's recycle bin is OFF** (`recycleBin: ''`). Still worth proposing.
7. **`interactive_search_max_candidates` is 1000.** The user's setting.
8. **`.mkv` files in the music library.** **Cloud LLM** wired but unused.
9. `staging.delete_source_folder_on_success` is **effectively false** via
   `webui_overrides.json` (`delete_originals_on_success` true).
10. **Recommend rotating the Prowlarr API key** (it was in old logs before
   CLI-08). Ask the owner; never rotate it yourself.
11. The owner sometimes **pauses the Lidarr container on purpose**; the
    pipeline then waits ("Lidarr not reachable ... no deadline"). Don't unpause.

---

## 7. Environment

| Thing | Value |
|---|---|
| PARK | `192.168.1.200`, key `C:\Users\zvani\.ssh\id_ed25519`, **bash over SSH** |
| Lidarr | `http://park:8686` (`park` resolves inside the container) |
| Prowlarr | `http://192.168.1.200:9696` |
| qBittorrent | `http://park:8080`, category **`lidarr`** — never touch others |
| LLM | `http://daniel:11434`, **HA's own** `huihui_ai/Qwen3.6-abliterated:27b`, `num_ctx 16384`, `think false` — one runner serves both. Asked only when the GPU gate is open (HA sensors via `HA_URL`/`HA_TOKEN`); `keep_alive` 600 never shortens HA's; `warmup_on_start` false |
| WebUI | `http://192.168.1.200:8830` |
| Container limits | `--cpuset-cpus 1,3,7,9 --cpus 2.0 --cpu-shares 256 --memory 4g` |

**PARK's CPU map is not what an Unraid template implies.** Only cores 1,3 (and
their siblings 7,9) are free: BIT owns 2,8,4,10,5,11, Home Assistant owns
4,10,5,11, CPU 0/6 serve the host and NVMe, and **CPU 1 carries the array's SATA
interrupts**. Check `virsh dumpxml "<vm>" | grep vcpupin` (quote the name — the
VM is called `Home Assistant`) and `/proc/interrupts` before pinning anything.
Pinning without a quota is what took the whole box down on 31 Aug.

Memory: `anon` sits near 1 GB and is flat; `docker stats` shows ~3 GB because
page cache counts toward the cgroup and is reclaimable. `memory.events` oom = 0.

**Standing instructions from the user:** fix things *in the pipeline*, never by
hand. Keep answers short. Always test your work, and check it.
