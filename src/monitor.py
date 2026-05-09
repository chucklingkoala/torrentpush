"""
torrentpush — watches a directory for .torrent files and uploads them to
a qBittorrent instance via its WebUI API (v4.1+).
"""

import logging
import os
import shutil
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configureLogging():
    """Send all logs to stdout so the Docker log driver captures them."""
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def loadConfig() -> dict:
    """
    Read configuration from environment variables and apply defaults.
    Raises SystemExit with a clear message if required variables are missing,
    so the container fails loudly at startup rather than on the first upload.
    """
    required = ["QB_HOST", "QB_USERNAME", "QB_PASSWORD"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        sys.exit(f"[ERROR] Missing required environment variables: {', '.join(missing)}")

    return {
        "qbHost":     os.environ["QB_HOST"].rstrip("/"),
        "qbUsername": os.environ["QB_USERNAME"],
        "qbPassword": os.environ["QB_PASSWORD"],
        "watchDir":   os.environ.get("WATCH_DIR", "/watch"),
        "savePath":   os.environ.get("QB_SAVE_PATH", ""),
        "category":   os.environ.get("QB_CATEGORY", ""),
        "tags":       os.environ.get("QB_TAGS", ""),
        "addPaused":  os.environ.get("QB_ADD_PAUSED", "false").lower() == "true",
        "retryDelay": int(os.environ.get("RETRY_DELAY", "5")),
        "maxRetries": int(os.environ.get("MAX_RETRIES", "3")),
    }


# ---------------------------------------------------------------------------
# qBittorrent API client
# ---------------------------------------------------------------------------

class QBittorrentClient:
    """
    Thin wrapper around the qBittorrent WebUI API.
    Uses a persistent requests.Session so the SID cookie is automatically
    attached to every request after login.
    """

    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.username = username
        self.password = password
        self.session = requests.Session()
        # qBittorrent's CSRF protection checks that Referer/Origin matches the
        # Host header.  Setting a default header here means every request
        # automatically passes that check without needing to set it each time.
        self.session.headers.update({
            "Referer": host,
            "Origin":  host,
        })

    def login(self):
        """Authenticate and store the SID cookie in the session."""
        url = f"{self.host}/api/v2/auth/login"
        resp = self.session.post(url, data={
            "username": self.username,
            "password": self.password,
        }, timeout=10)

        if resp.status_code == 403:
            raise RuntimeError(
                "Login failed: qBittorrent returned 403. "
                "Check QB_USERNAME and QB_PASSWORD."
            )
        resp.raise_for_status()

        # The API returns the plain string "Ok." on success.
        if resp.text.strip() != "Ok.":
            raise RuntimeError(f"Login failed: unexpected response: {resp.text!r}")

        logging.info("Logged in to qBittorrent at %s", self.host)

    def _ensureLoggedIn(self):
        """
        Probe the session with a lightweight request before every upload.
        Re-authenticates if the session has expired.

        Separating session-expiry from login errors prevents the retry loop
        in _uploadWithRetry from interpreting a stale session as a credential
        failure and giving up immediately.
        """
        try:
            resp = self.session.get(
                f"{self.host}/api/v2/app/version", timeout=10
            )
            # A valid session returns the version string; an expired one
            # returns "Forbidden" with a 403 status.
            if resp.status_code == 200 and resp.text.strip() != "Forbidden":
                return
        except requests.RequestException:
            pass  # network blip — fall through and attempt re-login

        logging.info("Session expired or unreachable — re-authenticating")
        self.login()

    def addTorrent(
        self,
        filePath: Path,
        savePath: str = "",
        category: str = "",
        tags: str = "",
        addPaused: bool = False,
    ):
        """
        Upload a .torrent file to qBittorrent.

        Raises:
            ValueError  — the file was rejected as an invalid torrent (HTTP 415).
            RuntimeError — authentication failure or other API error.
        """
        self._ensureLoggedIn()

        url = f"{self.host}/api/v2/torrents/add"

        # Build the multipart form.  The field name "torrents" is required by
        # the API; sending it under any other name silently fails.
        with filePath.open("rb") as fh:
            files = [("torrents", (filePath.name, fh, "application/x-bittorrent"))]

            data = {}
            if savePath:
                data["savepath"] = savePath
            if category:
                data["category"] = category
            if tags:
                data["tags"] = tags
            if addPaused:
                data["paused"] = "true"

            resp = self.session.post(url, files=files, data=data, timeout=30)

        if resp.status_code == 415:
            raise ValueError(
                f"qBittorrent rejected '{filePath.name}' as an invalid torrent file."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "Upload returned 403 after re-authentication — "
                "the account may lack write permissions."
            )
        resp.raise_for_status()

        logging.info("Uploaded '%s' successfully", filePath.name)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _waitForWriteComplete(path: Path, pollInterval: float = 0.5, stableFor: int = 2):
    """
    Spin until the file size stops changing.

    inotify (and watchdog on top of it) fires on_created as soon as the kernel
    creates the directory entry — which is before all bytes have been written,
    especially for large files or network-mounted shares.  Two consecutive
    identical non-zero size readings separated by pollInterval seconds are a
    reliable-enough proxy for "the writer is done."
    """
    previousSize = -1
    stableCount = 0
    while stableCount < stableFor:
        try:
            currentSize = path.stat().st_size
        except FileNotFoundError:
            # File disappeared between detection and check — nothing to do.
            return
        if currentSize == previousSize and currentSize > 0:
            stableCount += 1
        else:
            stableCount = 0
        previousSize = currentSize
        time.sleep(pollInterval)


def _moveFile(src: Path, destDir: Path):
    """
    Move src into destDir, appending a timestamp suffix on filename collision
    so earlier entries are never silently overwritten.
    """
    destDir.mkdir(parents=True, exist_ok=True)
    dest = destDir / src.name

    if dest.exists():
        # e.g. "my.torrent" → "my_20240301T123456.torrent"
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        dest = destDir / f"{src.stem}_{timestamp}{src.suffix}"

    shutil.move(str(src), str(dest))
    logging.info("Moved '%s' → '%s'", src.name, dest)


def _uploadWithRetry(client: QBittorrentClient, path: Path, config: dict):
    """
    Attempt to upload path to qBittorrent up to config['maxRetries'] times.

    On success the file is moved to processed/.
    On a hard failure (invalid torrent) the file is moved to failed/ immediately.
    On transient failure the function retries after config['retryDelay'] seconds;
    if all attempts are exhausted the file goes to failed/.
    """
    watchDir = Path(config["watchDir"])
    processedDir = watchDir / "processed"
    failedDir = watchDir / "failed"

    for attempt in range(1, config["maxRetries"] + 1):
        try:
            client.addTorrent(
                path,
                savePath=config["savePath"],
                category=config["category"],
                tags=config["tags"],
                addPaused=config["addPaused"],
            )
            _moveFile(path, processedDir)
            return

        except ValueError as exc:
            # Invalid torrent file — retrying won't help.
            logging.error("%s — moving to failed/", exc)
            _moveFile(path, failedDir)
            return

        except Exception as exc:
            logging.warning(
                "Upload attempt %d/%d failed for '%s': %s",
                attempt, config["maxRetries"], path.name, exc,
            )
            if attempt < config["maxRetries"]:
                time.sleep(config["retryDelay"])

    logging.error(
        "All %d upload attempts failed for '%s' — moving to failed/",
        config["maxRetries"], path.name,
    )
    _moveFile(path, failedDir)


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------

class TorrentEventHandler(FileSystemEventHandler):
    """Reacts to new files appearing in the watched directory."""

    def __init__(self, client: QBittorrentClient, config: dict):
        super().__init__()
        self.client = client
        self.config = config

    def on_created(self, event):
        """
        on_created rather than on_modified: the latter fires many times during
        a file write.  on_created fires exactly once when the directory entry
        appears, giving us a single clean trigger per file.
        """
        if event.is_directory:
            return

        path = Path(event.src_path)
        if path.suffix.lower() != ".torrent":
            return

        logging.info("Detected new torrent: '%s'", path.name)

        _waitForWriteComplete(path)

        if not path.exists():
            logging.warning("'%s' disappeared before upload — skipping", path.name)
            return

        _uploadWithRetry(self.client, path, self.config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    configureLogging()
    config = loadConfig()

    watchDir = Path(config["watchDir"])
    if not watchDir.is_dir():
        sys.exit(f"[ERROR] WATCH_DIR '{watchDir}' does not exist or is not a directory.")

    # Create output subdirectories up front so we don't race on first upload.
    (watchDir / "processed").mkdir(exist_ok=True)
    (watchDir / "failed").mkdir(exist_ok=True)

    client = QBittorrentClient(
        config["qbHost"],
        config["qbUsername"],
        config["qbPassword"],
    )

    # Fail fast: verify credentials before we start watching, rather than
    # letting the first upload attempt surface a bad-password error.
    client.login()

    handler = TorrentEventHandler(client, config)
    observer = Observer()

    # recursive=False is intentional: processed/ and failed/ live inside
    # watchDir, and recursing into them would re-trigger on every move.
    observer.schedule(handler, str(watchDir), recursive=False)
    observer.start()

    logging.info("Watching '%s' for .torrent files (Ctrl-C to stop)", watchDir)

    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
