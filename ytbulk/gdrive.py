"""Google Drive destination.

Why not rclone / a shared app: Google rate-limits every OAuth *app* (project).
Tools that ship one built-in app share that quota with all their users, so
uploads stall for minutes. ytbulk instead uses the user's OWN free Google Cloud
OAuth client ("Desktop app"), so the quota is theirs alone.

Robustness:
- resumable, chunked uploads; on network errors / 429 / 5xx / rate-limit 403
  it backs off exponentially and resumes from the last byte Drive confirmed
- MD5 of every upload is compared with the local file (corrupt -> re-upload)
- files already in the target folder (same name+size, or same [video id])
  are skipped, so re-runs and manual uploads never create duplicates
- clear, fatal errors for: sign-in expired, no permission, storage full,
  daily upload limit (750 GB/day per account)
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import queue
import random
import re
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("ytbulk")

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
CHUNK = 32 * 1024 * 1024              # must be a multiple of 256 KiB
MAX_RETRIES = 8
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
FILE_FIELDS = "id,name,size,md5Checksum,webViewLink"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


# ---------------------------------------------------------------- config / auth
def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ytbulk"


def client_file() -> Path:
    return config_dir() / "google_client_secret.json"


def token_file() -> Path:
    return config_dir() / "google_drive_token.json"


def libs_available() -> bool:
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        return True
    except ImportError:
        return False


def _write_private(path: Path, text: str) -> None:
    """Write a secret file readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def parse_folder_id(text: str) -> str | None:
    """Accepts a Drive folder URL (any variant) or a bare folder ID."""
    t = (text or "").strip()
    if t == "root":                      # "My Drive" from the folder picker
        return t
    for pat in (r"/folders/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})"):
        if m := re.search(pat, t):
            return m.group(1)
    return t if _ID_RE.match(t) else None


def save_client_secret(text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("That file is not valid JSON.") from None
    info = data.get("installed") if isinstance(data, dict) else None
    if not info:
        if isinstance(data, dict) and data.get("web"):
            raise ValueError("This OAuth client is a 'Web application'. Create one of type 'Desktop app'.")
        raise ValueError("Not an OAuth client file. Download the JSON of a 'Desktop app' OAuth client.")
    if not info.get("client_id") or not info.get("client_secret"):
        raise ValueError("The client file is missing client_id / client_secret.")
    _write_private(client_file(), json.dumps(data))


def load_credentials():
    """Saved credentials (refreshed if needed), or None if not connected / expired."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tf = token_file()
    if not tf.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(tf), SCOPES)
    except (ValueError, OSError):
        return None
    if creds.valid:
        return creds
    if not creds.refresh_token:
        return None
    try:
        creds.refresh(Request())
    except RefreshError:      # revoked, or 7-day expiry of apps left in "Testing"
        return None
    except requests.RequestException:
        return creds          # offline right now; let the caller's request fail clearly
    _write_private(tf, creds.to_json())
    return creds


def connect(timeout: int = 300):
    """Opens Google sign-in in the browser and stores the token (blocking)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file()), SCOPES)
    creds = flow.run_local_server(
        host="localhost", port=0, open_browser=True, timeout_seconds=timeout,
        authorization_prompt_message="Google sign-in opened in your browser. If not, visit:\n{url}",
        success_message="ytbulk is now connected to Google Drive. You can close this tab.",
        prompt="consent",
    )
    _write_private(token_file(), creds.to_json())
    return creds


def disconnect() -> None:
    tf = token_file()
    try:
        tok = json.loads(tf.read_text(encoding="utf-8"))
        token = tok.get("refresh_token") or tok.get("token")
        if token:
            requests.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=10)
    except (OSError, ValueError, requests.RequestException):
        pass
    tf.unlink(missing_ok=True)


def md5_of(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while block := f.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- errors
class DriveError(Exception):
    def __init__(self, message: str, category: str = "drive", hint: str | None = None, fatal: bool = False):
        super().__init__(message)
        self.category, self.hint, self.fatal = category, hint, fatal


class UploadStopped(Exception):
    pass


class _SessionExpired(Exception):
    pass


# ---------------------------------------------------------------- client
class DriveClient:
    def __init__(self, session, api: str = API, upload_url: str = UPLOAD_URL,
                 chunk: int = CHUNK, sleep=time.sleep):
        self.s = session
        self.api, self.upload_url, self.chunk, self._sleep = api, upload_url, chunk, sleep
        self.lock = threading.RLock()
        self._folders: dict[tuple, str | None] = {}
        self._listings: dict[str, list[dict]] = {}

    @classmethod
    def from_credentials(cls, creds) -> "DriveClient":
        from google.auth.transport.requests import AuthorizedSession
        return cls(AuthorizedSession(creds))

    # -- low level ------------------------------------------------------------
    @staticmethod
    def _reason(resp) -> tuple[str, str]:
        try:
            err = resp.json().get("error", {})
            errs = err.get("errors") or [{}]
            return (errs[0].get("reason") or err.get("status") or ""), (err.get("message") or "")
        except (ValueError, AttributeError):
            return "", (resp.text or "")[:200]

    def _retryable(self, resp) -> bool:
        if resp.status_code == 429 or resp.status_code >= 500:
            return True
        return resp.status_code == 403 and self._reason(resp)[0] in RATE_REASONS

    def _error(self, resp, what: str) -> DriveError:
        reason, msg = self._reason(resp)
        code = resp.status_code
        if code == 401:
            return DriveError("Google sign-in expired or was revoked.", "auth",
                              "Reconnect your Google account and start again.", fatal=True)
        if reason == "storageQuotaExceeded":
            return DriveError("The Google Drive storage is full.", "storage_full",
                              "Free up Drive space (or use another account), then start again.", fatal=True)
        if reason in ("uploadLimitExceeded", "dailyLimitExceeded", "quotaExceeded"):
            return DriveError("Google's daily upload limit for this account was reached (750 GB/day).",
                              "daily_limit", "Start again tomorrow - uploaded videos are skipped.", fatal=True)
        if code == 403:
            return DriveError(f"No permission: {msg or reason}", "permission",
                              "Make sure the signed-in account can add files to this Drive folder.", fatal=True)
        if code == 404:
            return DriveError("Drive folder not found, or not shared with the signed-in account.", "not_found",
                              "Check the folder link and the account you connected.", fatal=True)
        return DriveError(f"{what} failed: HTTP {code} {reason} {msg}".strip(), "drive")

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(64, 2 ** attempt) + random.random())

    def _call(self, method: str, url: str, what: str, ok=(200,), **kw):
        kw.setdefault("timeout", (20, 120))
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                log.warning("Drive %s: network error (%s), retry %d", what, type(e).__name__, attempt)
                self._backoff(attempt)
                continue
            if resp.status_code in ok:
                return resp
            if not self._retryable(resp):
                raise self._error(resp, what)
            log.warning("Drive %s: HTTP %d %s, retry %d", what, resp.status_code, self._reason(resp)[0], attempt)
            self._backoff(attempt)
        raise DriveError(f"Google Drive kept refusing ({what}) after {MAX_RETRIES} retries.", "rate_limited",
                         "Wait a few minutes and start again; finished uploads are skipped.")

    @staticmethod
    def _q(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    def _list(self, q: str, fields: str, corpora: str = "allDrives") -> list[dict]:
        out, token = [], None
        while True:
            params = {"q": q, "fields": f"nextPageToken,files({fields})", "pageSize": 1000, "corpora": corpora,
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self._call("GET", f"{self.api}/files", "list folder", params=params).json()
            out += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return out

    # -- account / folders ------------------------------------------------------
    def about(self) -> dict:
        d = self._call("GET", f"{self.api}/about", "account info",
                       params={"fields": "user(emailAddress,displayName),storageQuota(limit,usage)"}).json()
        q = d.get("storageQuota") or {}
        limit, usage = int(q.get("limit") or 0), int(q.get("usage") or 0)
        return {"email": (d.get("user") or {}).get("emailAddress"),
                "name": (d.get("user") or {}).get("displayName"),
                "free": (limit - usage) if limit else None}

    def folder_info(self, folder_id: str) -> dict:
        d = self._call("GET", f"{self.api}/files/{folder_id}", "check folder", params={
            "fields": "id,name,mimeType,trashed,capabilities(canAddChildren)", "supportsAllDrives": "true"}).json()
        if d.get("mimeType") != FOLDER_MIME:
            raise DriveError("That link points to a file, not a folder.", "not_folder", fatal=True)
        return {"id": d["id"], "name": d.get("name"), "trashed": bool(d.get("trashed")),
                "can_upload": bool((d.get("capabilities") or {}).get("canAddChildren"))}

    def list_folders(self, where: str) -> list[dict]:
        """Sub-folders for the Drive folder picker. where = 'root' (My Drive), 'shared'
        (Shared with me), 'drives' (Shared drives) or a folder / shared-drive id."""
        if where == "drives":
            out, token = [], None
            while True:
                params = {"pageSize": 100, "fields": "nextPageToken,drives(id,name)"}
                if token:
                    params["pageToken"] = token
                d = self._call("GET", f"{self.api}/drives", "list shared drives", params=params).json()
                out += [{"id": x["id"], "name": x.get("name") or "?"} for x in d.get("drives", [])]
                token = d.get("nextPageToken")
                if not token:
                    break
        elif where == "shared":
            out = self._list(f"sharedWithMe = true and mimeType = '{FOLDER_MIME}' and trashed = false",
                             "id,name", corpora="user")
        else:
            out = self._list(f"'{where}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false", "id,name")
        return sorted(({"id": f["id"], "name": f.get("name") or "?"} for f in out), key=lambda f: f["name"].lower())

    def create_folder(self, parent_id: str, name: str) -> dict:
        d = self._call("POST", f"{self.api}/files", "create folder",
                       params={"supportsAllDrives": "true", "fields": "id,name"},
                       json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}).json()
        with self.lock:
            self._folders[(parent_id, name)] = d["id"]
            self._listings[d["id"]] = []
        return {"id": d["id"], "name": d.get("name", name)}

    def find_folder(self, parent_id: str, name: str) -> str | None:
        key = (parent_id, name)
        with self.lock:
            if self._folders.get(key):
                return self._folders[key]
            found = self._list(f"name = '{self._q(name)}' and '{parent_id}' in parents and "
                               f"mimeType = '{FOLDER_MIME}' and trashed = false", "id,name")
            fid = found[0]["id"] if found else None
            if fid:
                self._folders[key] = fid
            return fid

    def ensure_folder(self, parent_id: str, name: str) -> str:
        with self.lock:
            fid = self.find_folder(parent_id, name)
            if fid:
                return fid
            fid = self._call("POST", f"{self.api}/files", "create folder",
                             params={"supportsAllDrives": "true", "fields": "id"},
                             json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}).json()["id"]
            self._folders[(parent_id, name)] = fid
            self._listings[fid] = []
            log.info("Drive: created folder %r", name)
            return fid

    def files_in(self, folder_id: str) -> list[dict]:
        with self.lock:
            if folder_id not in self._listings:
                self._listings[folder_id] = self._list(
                    f"'{folder_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'", FILE_FIELDS)
            return list(self._listings[folder_id])

    def find_existing(self, folder_id: str, name: str, size: int | None, video_id: str | None = None) -> dict | None:
        """Same name+size, or (when the size is unknown/equal) the same [video id] in the name."""
        tag = f"[{video_id}]" if video_id else None
        for f in self.files_in(folder_id):
            fsize = int(f["size"]) if f.get("size") else None
            same_size = size is None or fsize == size
            if same_size and (f.get("name") == name or (tag and tag in (f.get("name") or ""))):
                return f
        return None

    # -- upload -----------------------------------------------------------------
    def upload(self, path: Path, folder_id: str, on_progress=None, stop: threading.Event | None = None) -> dict:
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        local_md5 = md5_of(path)
        for attempt in (1, 2):
            uri = self._start_session(path.name, folder_id, size, mime)
            try:
                result = self._send(uri, path, size, on_progress, stop)
            except _SessionExpired:
                log.warning("Drive upload session expired for %s - restarting", path.name)
                continue
            remote_md5 = result.get("md5Checksum")
            if remote_md5 and remote_md5 != local_md5:
                log.error("Drive checksum mismatch for %s - re-uploading", path.name)
                self._call("DELETE", f"{self.api}/files/{result['id']}", "delete corrupt upload",
                           ok=(200, 204), params={"supportsAllDrives": "true"})
                continue
            with self.lock:
                if folder_id in self._listings:
                    self._listings[folder_id].append(result)
            return result
        raise DriveError(f"Upload of {path.name} failed verification twice.", "checksum")

    def _start_session(self, name: str, folder_id: str, size: int, mime: str) -> str:
        r = self._call("POST", self.upload_url, "start upload",
                       params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FILE_FIELDS},
                       json={"name": name, "parents": [folder_id]},
                       headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        uri = r.headers.get("Location")
        if not uri:
            raise DriveError("Google Drive did not return an upload session.", "drive")
        return uri

    @staticmethod
    def _range_end(resp) -> int:
        m = re.search(r"bytes=\d+-(\d+)", resp.headers.get("Range", ""))
        return int(m.group(1)) + 1 if m else 0

    def _query_offset(self, uri: str, size: int, fallback: int):
        try:
            r = self.s.request("PUT", uri, headers={"Content-Range": f"bytes */{size}"}, timeout=(20, 60))
        except requests.RequestException:
            return fallback
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code == 308:
            return self._range_end(r)
        if r.status_code in (404, 410):
            raise _SessionExpired()
        return fallback

    def _send(self, uri: str, path: Path, size: int, on_progress, stop) -> dict:
        offset, failures = 0, 0
        with path.open("rb") as f:
            while True:
                if stop is not None and stop.is_set():
                    raise UploadStopped()
                f.seek(offset)
                data = f.read(self.chunk)
                headers = ({"Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"}
                           if size else {"Content-Range": "bytes */0"})
                try:
                    r = self.s.request("PUT", uri, data=data, headers=headers, timeout=(20, 300))
                except requests.RequestException as e:
                    r, why = None, type(e).__name__
                else:
                    if r.status_code in (200, 201):
                        if on_progress:
                            on_progress(size, size)
                        return r.json()
                    if r.status_code == 308:
                        offset, failures = self._range_end(r), 0
                        if on_progress:
                            on_progress(offset, size)
                        continue
                    if r.status_code in (404, 410):
                        raise _SessionExpired()
                    if not self._retryable(r):
                        raise self._error(r, "upload")
                    why = f"HTTP {r.status_code} {self._reason(r)[0]}"
                failures += 1
                if failures > MAX_RETRIES:
                    raise DriveError(f"Upload kept failing ({why}).", "rate_limited",
                                     "Check the connection and start again; it resumes where it stopped.")
                log.warning("Drive upload %s: %s at byte %d, retry %d", path.name, why, offset, failures)
                self._backoff(failures)
                got = self._query_offset(uri, size, offset)
                if isinstance(got, dict):
                    if on_progress:
                        on_progress(size, size)
                    return got
                offset = got


# ---------------------------------------------------------------- worker
class UploadWorker(threading.Thread):
    """Uploads finished downloads one at a time while the next videos download."""

    def __init__(self, client: DriveClient, root_id: str, ui, stop: threading.Event,
                 delete_local: bool = False, on_fatal=None):
        super().__init__(daemon=True, name="drive-upload")
        self.client, self.root, self.ui, self.stop = client, root_id, ui, stop
        self.delete_local, self.on_fatal = delete_local, on_fatal
        self.q: queue.Queue = queue.Queue()
        self.fatal: DriveError | None = None

    def folder_for(self, section: str | None, create: bool) -> str | None:
        if not section:
            return self.root
        return self.client.ensure_folder(self.root, section) if create else self.client.find_folder(self.root, section)

    def already_on_drive(self, video_id: str, section: str | None) -> dict | None:
        """Used before downloading: is this video already in its Drive folder?"""
        if self.fatal:
            return None
        try:
            folder = self.folder_for(section, create=False)
            return self.client.find_existing(folder, "", None, video_id) if folder else None
        except DriveError as e:
            self._set_fatal(e)
            return None

    def submit(self, index: int, res) -> None:
        res.drive_status = "queued"
        self.q.put((index, res))

    def finish(self) -> None:
        self.q.put(None)

    def _set_fatal(self, e: DriveError) -> None:
        if e.fatal and not self.fatal:
            self.fatal = e
            log.error("Drive fatal error: %s", e)
            if self.on_fatal:
                self.on_fatal(e)

    def run(self) -> None:
        while (item := self.q.get()) is not None:
            index, res = item
            if self.fatal or self.stop.is_set():
                res.drive_status = "not_uploaded"
                res.drive_error = str(self.fatal) if self.fatal else "stopped before upload"
                self.ui.upload_done(index, res)
                continue
            self._one(index, res)

    def _one(self, index: int, res) -> None:
        path = Path(res.file) if res.file else None
        try:
            if not path or not path.is_file():
                raise DriveError("Local file is missing, nothing to upload.", "missing_file")
            size = path.stat().st_size
            folder = self.folder_for(res.section, create=True)
            existing = self.client.find_existing(folder, path.name, size, res.video_id)
            if existing:
                res.drive_status, res.drive_link = "already", existing.get("webViewLink")
                verified = existing.get("md5Checksum") in (None, "") or existing["md5Checksum"] == md5_of(path)
                if not verified:
                    res.drive_status = "already"   # same name+size but different content: keep the local file
                    res.drive_error = "A different file with the same name/size is already in the folder."
                ok_to_delete = verified and bool(existing.get("md5Checksum"))
            else:
                res.drive_status = "uploading"
                self.ui.upload_start(index, res, size)
                info = self.client.upload(path, folder, lambda sent, total: self.ui.upload_progress(index, sent, total),
                                          self.stop)
                res.drive_status, res.drive_link = "uploaded", info.get("webViewLink")
                ok_to_delete = True                  # upload() verified the MD5
                log.info("Drive OK %s -> %s", path.name, res.drive_link)
            if self.delete_local and ok_to_delete:
                path.unlink(missing_ok=True)
                res.local_deleted = True
        except UploadStopped:
            res.drive_status, res.drive_error = "not_uploaded", "stopped by user (re-run to finish)"
        except DriveError as e:
            res.drive_status, res.drive_error, res.drive_hint = "failed", str(e), e.hint
            log.error("Drive FAILED %s: %s", res.url, e)
            self._set_fatal(e)
        except Exception as e:  # noqa: BLE001 - report anything unexpected per video
            res.drive_status, res.drive_error = "failed", f"{type(e).__name__}: {e}"
            log.exception("Drive upload crashed for %s", res.url)
        self.ui.upload_done(index, res)


# ---------------------------------------------------------------- glue for CLI + web UI
def attach(dl, client: DriveClient, root_id: str, ui, delete_local: bool = False) -> UploadWorker:
    """Hook an UploadWorker into a BulkDownloader: skip videos already on Drive,
    queue every finished download for upload, stop downloading on fatal Drive errors."""
    def on_fatal(e: DriveError) -> None:
        dl.fatal = f"drive_{e.category}"

    worker = UploadWorker(client, root_id, ui, dl.stop_event, delete_local, on_fatal)

    def precheck(entry, res):
        found = worker.already_on_drive(entry.video_id, entry.section)
        if found:
            res.drive_status, res.drive_link = "already", found.get("webViewLink")
            res.title = res.title or found.get("name")
            return "already in the Drive folder"
        return None

    def on_result(index, res):
        if worker.fatal:
            res.drive_status, res.drive_error = "not_uploaded", str(worker.fatal)
            ui.upload_done(index, res)
        elif res.file:
            worker.submit(index, res)

    dl.precheck, dl.on_result = precheck, on_result
    worker.start()
    return worker


def wait(worker: UploadWorker) -> None:
    """Let queued uploads finish (or stop quickly if the stop event is set)."""
    worker.finish()
    while worker.is_alive():
        worker.join(0.5)
