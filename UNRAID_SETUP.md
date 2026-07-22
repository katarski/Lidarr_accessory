# Install cue_pipeline on Unraid

cue_pipeline runs as a Docker container **next to Lidarr**, in the same
`/downloads` + `/music` path namespace — so there's no SMB in the hot path and
no Windows↔container path translation.

There's no prebuilt image on a registry yet, so you build it locally **once**.
Every step below uses Unraid's built-in **web terminal** (the `>_` icon in the
top-right of the Unraid UI) — no SSH required.

---

## 1. Get the code onto Unraid

```sh
mkdir -p /mnt/user/appdata/cue_pipeline_src
cd /mnt/user/appdata/cue_pipeline_src
git clone https://github.com/katarski/Lidarr_accessory.git .
```

## 2. Build the image

```sh
cd /mnt/user/appdata/cue_pipeline_src
docker build -t cue_pipeline:latest .
docker images | grep cue_pipeline      # confirm the row exists before step 4
```

## 3. Create the config

```sh
mkdir -p /mnt/cache/appdata/cue_pipeline
cp config.example.yaml /mnt/cache/appdata/cue_pipeline/config.yaml
chown -R 99:100 /mnt/cache/appdata/cue_pipeline
```

Edit `/mnt/cache/appdata/cue_pipeline/config.yaml`:

- `lidarr.base_url` and `lidarr.api_key` (Lidarr → Settings → General → API Key)
- `qbittorrent.base_url` (+ `username`/`password` if your qBit isn't LAN-auth-bypassed)
- LLM: either leave `ollama.enabled: false`, or set `provider` / `base_url` /
  `model` / `api_key` (a free Gemini key works — see the example file).

Most behaviour knobs are also exposed as **container variables** in step 4,
and those override `config.yaml`.

## 4. Add the container

Docker tab → **Add Container**. In the **Template** field at the top, paste:

```
https://raw.githubusercontent.com/katarski/Lidarr_accessory/main/cue_pipeline.xml
```

That fills in the whole form. Set the three volume mounts to the **same host
paths Lidarr already uses** (this is the critical part — they must resolve to
the same files):

| Field | Set to | = Lidarr's |
|---|---|---|
| Config / appdata | `/mnt/cache/appdata/cue_pipeline` | (this app only) |
| Downloads | your finished-downloads share | Lidarr `/downloads` |
| Music library | your audio share (library = `/music/Music`) | Lidarr `/music` |

Fill in **Lidarr API key**, **qBittorrent password**, and **LLM API key** as
needed, then click **Apply**. Because the image was built locally in step 2, it
starts without trying to pull from a registry.

## 5. Verify

```sh
docker logs -f cue_pipeline
```

(or tail `/mnt/cache/appdata/cue_pipeline/pipeline.log`). You should see:

```
qBittorrent loop: enabled (every 30s, deselect=True, manage_completed=True, ...)
Watching /downloads
```

---

## Updating to a newer version

```sh
cd /mnt/user/appdata/cue_pipeline_src && git pull
docker build -t cue_pipeline:latest .
```

Then Docker tab → **cue_pipeline → Edit → Apply** to recreate the container on
the new image.

> ⚠️ **Rebuilding the image alone does NOT restart the running container.** The
> container was created from the *previous* image and keeps running it until you
> recreate it (Edit → Apply). If a new feature "isn't showing up", this is why.

---

## Key container variables (override `config.yaml`)

| Variable | Default | What it does |
|---|---|---|
| `LIDARR_URL` / `LIDARR_API_KEY` | — | Lidarr connection |
| `DELETE_SOURCE_FOLDER` | `true` | Delete the source folder after a verified import |
| `QBIT_URL` / `QBIT_USER` / `QBIT_PASS` | — | qBittorrent connection |
| `QBIT_AUTO_DESELECT` | `true` | On a discography torrent, skip albums already in the library |
| `QBIT_PAUSE_SCAN` | `true` | Pause a new torrent while deselecting, so owned albums never download |
| `QBIT_MANAGE_COMPLETED` | `true` | Pause a torrent mid-import; remove it (with data) once fully moved to the library |
| `QBIT_INTERVAL` | `30` | qBittorrent poll cadence (seconds, min 10) |
| `OLLAMA_ENABLED` / `LLM_*` | — | Optional LLM for `.cue` repair (Gemini/OpenAI/local Ollama) |
| `FORCE_IMPORT*` / `MIN_MATCH_PERCENT` | — | Force-import tuning for title mismatches |

---

## Notes

- The container runs as `--user 99:100` (nobody:users) and has **no WebUI** —
  it's a background worker. Manage it from the Docker tab.
- `/config` (appdata) holds `config.yaml`, the log, the ledger and audit files,
  so they persist across container restarts.
- The observer uses polling on purpose — `/mnt/user` is FUSE and inotify is
  unreliable there.
- The LLM is optional: it only auto-repairs malformed `.cue` files and
  cosmetically normalizes tags. `OLLAMA_ENABLED=false` turns it off entirely.
