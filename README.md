# torrentpush

Watches a directory for `.torrent` files and automatically uploads them to a qBittorrent instance via its WebUI API. Can run as a systemd service directly on Linux or as a Docker container.

---

## Prerequisites

- A running qBittorrent instance with the WebUI enabled
- Python 3.10+ (for systemd install) **or** Docker and Docker Compose (for container install)

---

## Configuration

All configuration is done via environment variables in `.env`.

```bash
cp .env.example .env
# edit .env with your qBittorrent details
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `QB_HOST` | Yes | — | qBittorrent WebUI URL, e.g. `http://localhost:8080` |
| `QB_USERNAME` | Yes | — | WebUI username |
| `QB_PASSWORD` | Yes | — | WebUI password |
| `WATCH_DIR` | No | `/watch` | Directory to monitor for `.torrent` files |
| `QB_SAVE_PATH` | No | _(QB default)_ | Override download save path in qBittorrent |
| `QB_CATEGORY` | No | _(none)_ | Category to assign uploaded torrents |
| `QB_TAGS` | No | _(none)_ | Comma-separated tags to assign |
| `QB_ADD_PAUSED` | No | `false` | Add torrents in paused state (`true`/`false`) |
| `RETRY_DELAY` | No | `5` | Seconds between upload retry attempts |
| `MAX_RETRIES` | No | `3` | Max upload attempts before moving to `failed/` |

---

## Option A — systemd service (run directly on Linux)

### 1. Install

```bash
git clone https://github.com/chucklingkoala/torrentpush.git /opt/torrentpush
cd /opt/torrentpush
cp .env.example .env
```

Edit `.env` — set `QB_HOST`, `QB_USERNAME`, `QB_PASSWORD`, and `WATCH_DIR` to the folder you want to monitor.

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv /opt/torrentpush/venv
/opt/torrentpush/venv/bin/pip install -r /opt/torrentpush/requirements.txt
```

### 3. Configure the systemd unit

Edit `torrentpush.service` and update the `User=` and `WorkingDirectory=` fields if you installed somewhere other than `/opt/torrentpush`, then copy it to systemd:

```bash
sudo cp /opt/torrentpush/torrentpush.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now torrentpush
```

### 4. Check status and logs

```bash
sudo systemctl status torrentpush
journalctl -u torrentpush -f
```

### Managing the service

```bash
sudo systemctl stop torrentpush     # stop
sudo systemctl start torrentpush    # start
sudo systemctl restart torrentpush  # restart after config changes
```

---

## Option B — Docker Compose

### 1. Install

```bash
git clone https://github.com/chucklingkoala/torrentpush.git
cd torrentpush
cp .env.example .env
```

Edit `.env` with your qBittorrent details.

### 2. Configure the watch volume

Edit `docker-compose.yml` and update the volume mount to point at the host directory you want to watch:

```yaml
volumes:
  - /your/host/path:/watch
```

### 3. Start the service

```bash
# Create the nginx-bridge network if it doesn't exist yet
docker network create nginx-bridge

docker compose up -d
```

### 4. Check logs

```bash
docker logs torrentpush -f
```

---

## Usage

Drop any `.torrent` file into the watched directory. torrentpush detects it automatically and uploads it to qBittorrent within a second or two.

Example log output:

```
2024-03-01T12:00:00 [INFO] Logged in to qBittorrent at http://localhost:8080
2024-03-01T12:00:00 [INFO] Watching '/data/torrents/inbox' for .torrent files (Ctrl-C to stop)
2024-03-01T12:01:15 [INFO] Detected new torrent: 'ubuntu-24.04.torrent'
2024-03-01T12:01:16 [INFO] Uploaded 'ubuntu-24.04.torrent' successfully
2024-03-01T12:01:16 [INFO] Moved 'ubuntu-24.04.torrent' → '/data/torrents/inbox/processed/ubuntu-24.04.torrent'
```

### Directory layout

```
<WATCH_DIR>/
├── *.torrent          ← drop files here
├── processed/         ← successfully uploaded torrents are moved here
└── failed/            ← torrents that could not be uploaded are moved here
```

`processed/` and `failed/` are created automatically on startup. Files in `failed/` can be re-submitted by moving them back to the watch root.

---

## Networking

| Setup | `QB_HOST` value |
|---|---|
| qBittorrent on the same machine | `http://localhost:8080` |
| qBittorrent on another machine | `http://192.168.1.100:8080` |
| qBittorrent in Docker on `nginx-bridge` | `http://qbittorrent:8080` |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `403` at startup | Wrong credentials | Check `QB_USERNAME` / `QB_PASSWORD` in `.env` |
| Torrent moves to `failed/` immediately | Invalid or corrupt `.torrent` file | Verify the file opens in a torrent client |
| Files are not detected | `WATCH_DIR` path doesn't exist or is wrong | Confirm the path exists and the service user can read it |
| Files not detected on NFS/CIFS mounts | inotify not supported on network filesystems | Falls back to polling automatically — detection may be slightly delayed |
| Service fails to start | Wrong Python path in unit file | Confirm `venv/bin/python` exists under `WorkingDirectory` |
