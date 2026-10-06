#!/usr/bin/env python3
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger("kavita-prewarm")


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str
    forward_chapters: int
    interval_seconds: int
    cooldown_seconds: int
    state_path: str
    timeout_seconds: int

    @classmethod
    def from_env(cls, env=os.environ):
        url = env.get("KAVITA_URL", "").rstrip("/")
        key = env.get("KAVITA_API_KEY", "")
        forward = int(env.get("FORWARD_CHAPTERS", "2"))
        interval = int(env.get("INTERVAL_SECONDS", "300"))
        cooldown = int(env.get("COOLDOWN_SECONDS", "21600"))
        timeout = int(env.get("HTTP_TIMEOUT_SECONDS", "30"))
        if not url or not key or forward < 0 or interval < 1 or cooldown < 0 or timeout < 1:
            raise ValueError("invalid configuration")
        return cls(url, key, forward, interval, cooldown,
                   env.get("STATE_PATH", "/state/state.json"), timeout)


class UrllibTransport:
    def request(self, method, url, headers=None, body=None, timeout=15):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()


class KavitaClient:
    def __init__(self, base_url, api_key, transport=None, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.configured_api_key = api_key
        self.api_key = api_key
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.jwt = None

    def login(self):
        body = json.dumps({"apiKey": self.configured_api_key, "username": "", "password": ""}).encode()
        data = self._request("POST", "/api/Account/login", body=body, authenticated=False)
        self.jwt = data["token"]
        self.api_key = data.get("apiKey") or self.configured_api_key

    def _request(self, method, path, body=None, authenticated=True, binary=False):
        if authenticated and self.jwt is None:
            self.login()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = "Bearer " + self.jwt
        status, _, payload = self.transport.request(method, self.base_url + path, headers, body, self.timeout)
        if status >= 400:
            raise urllib.error.HTTPError(path, status, "HTTP error", {}, None)
        return payload if binary else json.loads(payload)

    def on_deck(self):
        return self._request("POST", "/api/Series/on-deck?PageNumber=1&PageSize=50&libraryId=0",
                             body=b"{}")

    def volumes(self, series_id):
        return self._request("GET", "/api/Series/volumes?" + urllib.parse.urlencode({"seriesId": series_id}))

    def prewarm(self, chapter_id):
        query = urllib.parse.urlencode({"chapterId": chapter_id, "page": 0, "apiKey": self.api_key})
        return self._request("GET", "/api/Reader/image?" + query, binary=True)


def choose_chapters(volumes, forward_chapters):
    chapters = [chapter for volume in volumes for chapter in (volume.get("chapters") or [])
                if chapter.get("id") is not None and chapter.get("pages", 0) > 0]
    chapters.sort(key=lambda c: (c.get("sortOrder", c.get("minNumber", 0)), c["id"]))
    partials = [i for i, c in enumerate(chapters) if 0 < c.get("pagesRead", 0) < c["pages"]]
    reads = [i for i, c in enumerate(chapters) if c.get("pagesRead", 0) > 0]
    if not reads:
        return []
    anchor = partials[-1] if partials else reads[-1]
    return chapters[anchor:anchor + forward_chapters + 1]


class StateStore:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as stream:
                self.data = json.load(stream)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.data = {}

    def last(self, chapter_id):
        return self.data.get(str(chapter_id), 0)

    def mark(self, chapter_id, timestamp):
        self.data[str(chapter_id)] = timestamp
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class Prewarmer:
    def __init__(self, client, state, forward_chapters, cooldown_seconds, clock=time.time):
        self.client, self.state = client, state
        self.forward_chapters, self.cooldown_seconds = forward_chapters, cooldown_seconds
        self.clock = clock

    def run_once(self):
        warmed = 0
        try:
            series_list = self.client.on_deck()
        except Exception as error:
            LOG.error("on-deck failed: %s", error)
            return 0
        for series in series_list or []:
            series_id = series.get("id")
            if series_id is None:
                LOG.warning("skipping on-deck entry without id")
                continue
            try:
                selected = choose_chapters(self.client.volumes(series_id), self.forward_chapters)
            except Exception as error:
                LOG.warning("skipping series %s: %s", series_id, error)
                continue
            for chapter in selected:
                chapter_id = chapter["id"]
                now = self.clock()
                if now - self.state.last(chapter_id) < self.cooldown_seconds:
                    LOG.info("chapter %s still in cooldown", chapter_id)
                    continue
                try:
                    self.client.prewarm(chapter_id)
                except urllib.error.HTTPError as error:
                    LOG.warning("skipping chapter %s: HTTP %s", chapter_id, error.code)
                    continue
                except Exception as error:
                    LOG.warning("skipping chapter %s: %s", chapter_id, error)
                    continue
                self.state.mark(chapter_id, now)
                warmed += 1
                LOG.info("prewarmed chapter %s", chapter_id)
        return warmed


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env()
    worker = Prewarmer(KavitaClient(config.url, config.api_key, timeout=config.timeout_seconds),
                       StateStore(config.state_path), config.forward_chapters, config.cooldown_seconds)
    while True:
        worker.run_once()  # ponytail: single process/worker; add a shared lock only if replicas are required.
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
