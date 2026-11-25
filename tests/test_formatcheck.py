"""포맷 전 점검: 기록과 저장 폴더만 보고, 이전 세션에서 꺼낸 파일까지 합쳐서 판단한다."""
import json
import os
import random
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from apfsrecover import extract as ex
from apfsrecover import verify as vf
from apfsrecover.server import App, Job
from . import synth
from .test_formats_more import make_pdf

PHOTO = ["jpeg", "heic", "raw-cr3", "video-mp4", "video-mov"]


class FormatCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = random.Random(4)
        self.img = os.path.join(self.tmp.name, "d.img")
        synth.build_image(self.img, [(2, synth.make_jpeg(r, 600_000)), (300, synth.make_mp4(r, 900_000, datetime(2024, 1, 2))),
                                     (700, make_pdf(r, "plain"))], 1200)
        self.dest = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, name, kinds, reuse=None):
        a = App(os.path.join(self.tmp.name, name), images={"demo": self.img}, sample_step=64 * 1024)
        dev = a.devices()[0]
        self.assertEqual(a._scan(Job("scan"), dev, False, False, reuse=reuse)[0], "done")
        plan = {"dest": self.dest, "kinds": kinds, "include_suspect": True, "batch": {"unit": "n", "size": 10 ** 9}}
        self.assertEqual(ex.extract_batch(dev["path"], a.session, plan)["state"], "done")
        vf.verify_session(a.session, self.dest, deep=False)  # 가짜 사진은 끝까지 디코딩되지 않으므로 구조만 본다
        return a, dev

    def level(self, r):
        return [x[0] for x in r["items"]]

    def test_photos_in_old_session_and_docs_in_new_count_together(self):
        old, _ = self.app("session_old", PHOTO)
        new, dev = self.app("session", ["pdf"], reuse=old.session)
        r = new.format_check(dev)
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(len(r["items"]), 7)
        self.assertEqual(self.level(r)[:5], ["ok"] * 5, r["items"])      # 옛 세션의 사진도 '꺼냄'으로 센다
        self.assertEqual(r["items"][6][0], "ok")                         # 작은 가짜 디스크는 끝까지 검사됨

    def test_only_new_session_reports_unextracted_photos(self):
        new, dev = self.app("session", ["pdf"])
        r = new.format_check(dev)
        self.assertEqual(r["items"][4][0], "warn")
        self.assertIn("꺼내지 않은 파일 2개", r["items"][4][1])

    def test_missing_file_and_unplugged_disk_warn(self):
        a, dev = self.app("session", PHOTO + ["pdf"])
        first = next(iter(ex.load_extracted(a.session).values()))
        os.remove(os.path.join(self.dest, first["path"]))
        r = a.format_check(None)
        self.assertEqual(r["items"][0][0], "warn")
        self.assertEqual(r["items"][2][0], "warn")
        self.assertEqual(r["verdict"], "warn")

    def test_dest_on_source_disk_is_bad(self):
        a, dev = self.app("session", PHOTO + ["pdf"])
        with mock.patch.object(App, "forbidden_mounts", return_value=[self.tmp.name]):
            r = a.format_check(dev)
        self.assertEqual((r["items"][0][0], r["verdict"]), ("bad", "bad"))

    def test_pending_files_and_no_scan(self):
        a, dev = self.app("session", ["pdf"])
        p = os.path.join(a.session, "plan.json")
        with open(p, encoding="utf-8") as f:
            plan = json.load(f)
        plan["kinds"] = PHOTO + ["pdf"]                                  # 계획을 넓혔는데 아직 안 꺼냄
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f)
        r = a.format_check(dev)
        self.assertEqual((r["items"][1][0], r["verdict"]), ("bad", "bad"))
        empty = App(os.path.join(self.tmp.name, "none"), images={"demo": self.img})
        self.assertEqual(empty.format_check(None)["verdict"], "bad")


if __name__ == "__main__":
    unittest.main()
