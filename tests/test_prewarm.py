import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from kavita_prewarm import Config, KavitaClient, Prewarmer, StateStore, choose_chapters


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append((method, url, headers or {}, body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ConfigTests(unittest.TestCase):
    def test_config_validates_required_and_numeric_values(self):
        env = {"KAVITA_URL": "http://kavita:5000/", "KAVITA_API_KEY": "secret",
               "FORWARD_CHAPTERS": "2", "INTERVAL_SECONDS": "30", "COOLDOWN_SECONDS": "60"}
        cfg = Config.from_env(env)
        self.assertEqual((cfg.url, cfg.forward_chapters, cfg.interval_seconds),
                         ("http://kavita:5000", 2, 30))
        with self.assertRaises(ValueError):
            Config.from_env({"KAVITA_URL": "x", "KAVITA_API_KEY": "", "FORWARD_CHAPTERS": "0"})


class SelectionTests(unittest.TestCase):
    def test_partial_chapter_then_next_available_chapters_across_volumes(self):
        volumes = [{"chapters": [
            {"id": 10, "sortOrder": 1, "pages": 20, "pagesRead": 20},
            {"id": 12, "sortOrder": 3, "pages": 20, "pagesRead": 5},
        ]}, {"chapters": [
            {"id": 15, "sortOrder": 5, "pages": 0, "pagesRead": 0},
            {"id": 16, "sortOrder": 6, "pages": 20, "pagesRead": 0},
            {"id": 18, "sortOrder": 8, "pages": 20, "pagesRead": 0},
        ]}]
        self.assertEqual([c["id"] for c in choose_chapters(volumes, 2)], [12, 16, 18])

    def test_latest_read_chapter_is_anchor_when_none_partial(self):
        volumes = [{"chapters": [
            {"id": 1, "sortOrder": 1, "pages": 10, "pagesRead": 10},
            {"id": 2, "sortOrder": 2, "pages": 10, "pagesRead": 10},
            {"id": 3, "sortOrder": 4, "pages": 10, "pagesRead": 0},
        ]}]
        self.assertEqual([c["id"] for c in choose_chapters(volumes, 1)], [2, 3])

    def test_series_without_read_progress_selects_nothing(self):
        volumes = [{"chapters": [{"id": 1, "sortOrder": 1, "pages": 10, "pagesRead": 0}]}]
        self.assertEqual(choose_chapters(volumes, 2), [])


class ClientTests(unittest.TestCase):
    def test_api_contract_matches_kavita_084(self):
        transport = FakeTransport([
            (200, {}, b'{"token":"jwt","apiKey":"returned"}'),
            (200, {}, b'[{"id":7}]'),
            (200, {}, b'[{"chapters":[]}]'),
            (200, {"content-type": "image/jpeg"}, b'image'),
        ])
        client = KavitaClient("http://k", "configured", transport)
        self.assertEqual(client.on_deck(), [{"id": 7}])
        self.assertEqual(client.volumes(7), [{"chapters": []}])
        client.prewarm(9)
        self.assertEqual(json.loads(transport.calls[0][3]),
                         {"apiKey": "configured", "username": "", "password": ""})
        self.assertIn("/api/Series/on-deck?PageNumber=1&PageSize=50&libraryId=0", transport.calls[1][1])
        self.assertEqual(transport.calls[1][0], "POST")
        self.assertIn("/api/Series/volumes?seriesId=7", transport.calls[2][1])
        self.assertIn("/api/Reader/image?chapterId=9&page=0&apiKey=returned", transport.calls[3][1])


class StateTests(unittest.TestCase):
    def test_state_is_atomic_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            store = StateStore(path)
            store.mark(4, 123.5)
            self.assertEqual(StateStore(path).last(4), 123.5)
            self.assertFalse(os.path.exists(path + ".tmp"))


class RunnerTests(unittest.TestCase):
    def test_cooldown_and_404_skip_do_not_stop_other_chapters(self):
        class Client:
            def on_deck(self): return [{"id": 1}]
            def volumes(self, _):
                return [{"chapters": [
                    {"id": 1, "sortOrder": 1, "pages": 10, "pagesRead": 5},
                    {"id": 2, "sortOrder": 2, "pages": 10, "pagesRead": 0},
                    {"id": 3, "sortOrder": 3, "pages": 10, "pagesRead": 0},
                ]}]
            def prewarm(self, chapter_id):
                calls.append(chapter_id)
                if chapter_id == 2:
                    raise urllib.error.HTTPError("u", 404, "missing", {}, None)
        calls = []
        with tempfile.TemporaryDirectory() as d:
            state = StateStore(os.path.join(d, "state.json"))
            state.mark(1, 90)
            worker = Prewarmer(Client(), state, forward_chapters=2, cooldown_seconds=20, clock=lambda: 100)
            self.assertEqual(worker.run_once(), 1)
            self.assertEqual(calls, [2, 3])
            self.assertEqual(state.last(3), 100)
            self.assertEqual(state.last(2), 0)

    def test_absent_series_or_volume_404_does_not_stop_poll(self):
        missing = urllib.error.HTTPError("u", 404, "gone", {}, None)
        class Client:
            def on_deck(self): return [{"id": 1}, {"id": 2}]
            def volumes(self, sid):
                if sid == 1: raise missing
                return []
        with tempfile.TemporaryDirectory() as d:
            worker = Prewarmer(Client(), StateStore(os.path.join(d, "s.json")), 1, 20)
            self.assertEqual(worker.run_once(), 0)


if __name__ == "__main__":
    unittest.main()
