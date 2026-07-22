# cue_pipeline on Unraid

Runs the pipeline in a container next to Lidarr, in the same path namespace
(`/downloads`, `/music`) — no SMB, no Windows↔container path translation.

## 1. Put the code on Unraid
Copy this whole folder to the server, e.g. `/mnt/user/appdata/cue_pipeline_src/`
(or `git clone`). It needs: `Dockerfile`, `requirements.txt`, all `*.py`,
`docker-compose.yml`.

## 2. Create the appdata / config dir
```sh
mkdir -p /mnt/cache/appdata/cue_pipeline
cp config.example.yaml /mnt/cache/appdata/cue_pipeline/config.yaml
chown -R 99:100 /mnt/cache/appdata/cue_pipeline
```
Edit `/mnt/cache/appdata/cue_pipeline/config.yaml` if any path/URL differs.

## 3. Match the volume mounts to Lidarr
In `docker-compose.yml`, the LEFT side of each mount must be the **host path
Lidarr already uses**. Check Lidarr's own container mappings and mirror them:

| Container path | Must point at (host) | = Lidarr's |
|---|---|---|
| `/downloads` | your Internet Downloads share | Lidarr `/downloads` |
| `/music`     | your Audio share (library = `/music/Music`) | Lidarr `/music` |
| `/config`    | `/mnt/cache/appdata/cue_pipeline` | (this app only) |

If Lidarr's `/downloads` points somewhere else, change the left side to match
— they MUST resolve to the same files.

## 4. Build & run
```sh
cd /path/to/cue_pipeline_src
docker compose build
docker compose up -d
docker compose logs -f        # watch it start
```
(Or add via the Unraid **Docker** tab: build the image, then map the 3 volumes
and set `user` 99:100, `TZ`.)

## 5. Sanity checks
- `docker compose logs -f` should show: Ollama reachable at 192.168.1.32:11434
  (or a warning if not — harmless, LLM is optional), Lidarr reachable, then the
  startup scan.
- Confirm it sees the library: it should NOT log path errors for `/music/Music`.

## Notes
- **LLM is optional.** It only auto-repairs malformed cue files and cosmetically
  normalizes tags. To run without it: set `ollama.enabled: false`. Otherwise it
  uses the RTX box at `192.168.1.32:11434`.
- **Library audit** is off by default; enable with `library_audit_enabled: true`.
  It runs on a schedule and only walks the library when it changed.
- Logs/ledger/audit CSV live in `/config` (appdata) so they persist across
  container restarts.
- `observer: polling` is set on purpose — `/mnt/user` is FUSE and inotify is
  unreliable there. Polling local disk is cheap.
