# cue_pipeline — handoff

Consolidated 17 Sep 2026. This file is rewritten, not appended to: everything
below is current. `README.md` explains what the pipeline does; this is what a
fresh session needs — how to deploy, what is true now, what is still open, and
the mistakes worth not repeating.

---

## 1. Deployment (use exactly this)

The image is **built locally on PARK and exists in no registry**. Every code
change needs a rebuild AND a container replace — `docker restart` keeps the old
image.

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
- **Nothing is grabbed for an unmonitored album or artist** — Lidarr will not
  import into one either, so the download is wasted twice. Fails open: no id, or
  a lookup error, and the grab proceeds.
- **prefer-lossless quarantines, never deletes**, and cannot nest: quarantined
  paths are excluded both at the audit and inside the mover. It once nested
  `_superseded_by_lossless` 71 levels deep, which made a Jellyfin validation
  worker spin a core for 13 h.
- `replace_existing` **does not exist** on `LidarrClient` — see §5.

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
- **Do not read a rate off progress lines.** The gap between two
  "[n/758] discrepancy" lines is filled with searches, imports and harvests, not
  scanning — which is how the audit got blamed for 50 minutes of other work.

---

## 6. Open items

1. **Lidarr's recycle bin is OFF** (`recycleBin: ''`). Turning it on would have
   made the Frida loss recoverable. Still worth proposing.
2. **`interactive_search_max_candidates` is 1000.** One bad artist match becomes
   1000 grab attempts. 5–10 would be saner. The user's setting; not changed.
3. **Albums still partially imported.** The audit works through them; watch
   `under-registered` lines. Not all are fixable.
4. **Orphan uncategorised torrents** in qBittorrent carry no category and no
   history rows — they escape every category-scoped guard.
5. **`.mkv` files in the music library.**
6. **Cloud LLM** (`cloud_llm.py`) is wired but unused.
7. **The Klayton torrent** (84%, 0 seeders, fills no gap) — a one-off cleanup.
8. `staging.delete_source_folder_on_success` is **effectively false** via
   `webui_overrides.json` while the YAML and env both say true. The guard in §3
   means that override is no longer the only thing preventing data loss, so it
   can be turned back on when wanted.

---

## 7. Environment

| Thing | Value |
|---|---|
| PARK | `192.168.1.200`, key `C:\Users\zvani\.ssh\id_ed25519`, **bash over SSH** |
| Lidarr | `http://192.168.1.200:8686` |
| Prowlarr | `http://192.168.1.200:9696` |
| qBittorrent | `http://192.168.1.200:8080`, category **`lidarr`** — never touch others |
| LLM | `http://daniel:11434`, `huihui_ai/qwen2.5-abliterate:14b`. GPU **shared with the user's own work** — `keep_alive` is 5m and `warmup_on_start` is false on purpose |
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
