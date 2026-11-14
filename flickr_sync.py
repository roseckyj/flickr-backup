#!/usr/bin/env python3
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from croniter import croniter

API_URL = "https://api.flickr.com/services/rest/"
CONFIG_DIR = Path("/root/.config/flickrdownloader")
STATE_FILE = CONFIG_DIR / "state.json"
LOCK_FILE = CONFIG_DIR / "sync.lock"
OUTPUT_DIR = Path("/photos")


def encoded(value):
    return urllib.parse.quote(str(value), safe="~-._")


def oauth_request(method, params):
    oauth = {
        "oauth_consumer_key": os.environ["FLICKR_API_KEY"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    token = os.environ.get("FLICKR_AUTH_TOKEN")
    if token:
        oauth["oauth_token"] = token
    all_params = {**params, **oauth}
    normalized = "&".join(
        f"{encoded(k)}={encoded(v)}"
        for k, v in sorted(all_params.items(), key=lambda item: (encoded(item[0]), encoded(item[1])))
    )
    base = "&".join((method.upper(), encoded(API_URL), encoded(normalized)))
    key = f"{encoded(os.environ['FLICKR_API_SECRET'])}&{encoded(os.environ.get('FLICKR_AUTH_TOKEN_SECRET', ''))}"
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    query = urllib.parse.urlencode({**params, **oauth})
    with urllib.request.urlopen(f"{API_URL}?{query}", timeout=60) as response:
        payload = json.load(response)
    if payload.get("stat") != "ok":
        raise RuntimeError(f"Flickr API error: {payload.get('message', 'unknown error')}")
    return payload


def flickr(method, **params):
    return oauth_request("GET", {"method": method, "format": "json", "nojsoncallback": 1, **params})


def owner_id():
    username = os.environ.get("FLICKR_USERNAME")
    url = os.environ.get("FLICKR_URL", "")
    if url:
        return flickr("flickr.urls.lookupUser", url=url)["user"]["id"]
    if username and (username.startswith("http://") or username.startswith("https://")):
        return flickr("flickr.urls.lookupUser", url=username)["user"]["id"]
    if username:
        username = username.strip().strip("/")
    if username and "@" in username:
        return flickr("flickr.people.getInfo", user_id=username)["person"]["id"]
    if not username:
        raise RuntimeError("FLICKR_USERNAME or FLICKR_URL is required")
    return flickr(
        "flickr.urls.lookupUser",
        url=f"https://www.flickr.com/photos/{urllib.parse.quote(username)}/",
    )["user"]["id"]


def albums(user_id):
    page = 1
    while True:
        result = flickr("flickr.photosets.getList", user_id=user_id, page=page, per_page=500)
        photosets = result["photosets"]
        yield from photosets.get("photoset", [])
        if page >= int(photosets.get("pages", 1)):
            return
        page += 1


def album_photos(user_id, album_id):
    page = 1
    while True:
        result = flickr(
            "flickr.photosets.getPhotos",
            user_id=user_id,
            photoset_id=album_id,
            page=page,
            per_page=500,
            extras="url_o,date_upload",
        )
        photos = result["photoset"]
        yield from photos.get("photo", [])
        if page >= int(photos.get("pages", 1)):
            return
        page += 1


def safe_name(name):
    name = re.sub(r"[^\w .()#+=-]+", "_", name, flags=re.UNICODE).strip(" .")
    return name or "untitled"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=CONFIG_DIR, prefix="state-", text=True)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temp, STATE_FILE)


def download(url, target):
    time.sleep(float(os.environ.get("DOWNLOAD_DELAY", "1")))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    os.close(fd)
    try:
        for attempt in range(8):
            result = subprocess.run(
                [
                    "wget", "--no-verbose", "-4", "--timeout=60", "--tries=1",
                    f"--user-agent={os.environ.get('DOWNLOAD_USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36')}",
                    "-O", temp, url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                break
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown wget error"
            if "429" in result.stderr:
                delay = float(os.environ.get("RATE_LIMIT_DELAY", "60"))
                print(f"rate limited; retrying {url} in {delay:g}s", flush=True)
                time.sleep(delay)
            elif attempt < 7:
                time.sleep(min(30 * (2**attempt), 300))
            else:
                raise RuntimeError(f"wget exit {result.returncode}: {detail}")
        if result.returncode:
            raise RuntimeError(f"wget exit {result.returncode}: {detail}")
        if Path(temp).stat().st_size == 0:
            raise RuntimeError("downloaded file is empty")
        with open(temp, "rb") as stream:
            if stream.read(512).lstrip().lower().startswith((b"<html", b"<!doctype html")):
                raise RuntimeError("downloaded HTML instead of media")
        os.replace(temp, target)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _sync_once():
    user_id = owner_id()
    state = load_state()
    total = downloaded = skipped = failed = 0
    for album in albums(user_id):
        album_dir = OUTPUT_DIR / safe_name(album["title"]["_content"])
        for photo in album_photos(user_id, album["id"]):
            total += 1
            url = photo.get("url_o")
            if not url:
                continue
            target = album_dir / f"{photo['id']}.jpg"
            key = f"{album['id']}:{photo['id']}"
            if target.is_file() and target.stat().st_size > 0 and state.get(key) == str(target):
                skipped += 1
                continue
            try:
                print(f"downloading {photo['id']}...", flush=True)
                download(url, target)
                state[key] = str(target)
                save_state(state)
                downloaded += 1
                print(f"downloaded {target}", flush=True)
            except Exception as error:
                failed += 1
                print(f"failed {photo['id']}: {error}", file=sys.stderr, flush=True)
    print(f"sync complete: {downloaded} downloaded, {skipped} skipped, {failed} failed, {total} photos")
    return 1 if failed else 0


def sync_once():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("sync already running; skipping this invocation", flush=True)
            return 0
        return _sync_once()


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "run-now":
        return sync_once()
    if sys.argv[1] == "watch":
        schedule = os.environ.get("CRON_SCHEDULE", "0 3 * * *")
        try:
            croniter(schedule)
        except (ValueError, KeyError) as error:
            raise RuntimeError(f"invalid CRON_SCHEDULE: {schedule}") from error
        while True:
            sync_once()
            now = time.localtime()
            next_run = croniter(schedule, time.mktime(now)).get_next(float)
            delay = max(1, next_run - time.time())
            print(f"next sync in {delay:.0f}s ({schedule})", flush=True)
            time.sleep(delay)
    raise SystemExit(f"unsupported command: {sys.argv[1]}")


if __name__ == "__main__":
    raise SystemExit(main())
