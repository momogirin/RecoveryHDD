import hashlib
import json
import os
import random
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

from apfsrecover import catalog as cat
from apfsrecover import extract as ex
from apfsrecover import guards
from . import synth
from .synth import BS


class ExtractBase(unittest.TestCase):
    def setUp(self):
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = random.Random(7)
        t = synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35")
        self.files = {
            1: synth.make_jpeg(r, 300_000, t),
            100: synth.make_jpeg(r, 250_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:07 10:00:00")),
            300: synth.make_jpeg(r, 120_000),
            500: synth.make_mp4(r, 900_000, datetime(2024, 3, 6, 16, 30, 0)),   # UTC 16:30 = KST 다음날 01:30
            900: synth.make_mp4(r, 400_000, datetime(2024, 3, 6), moov=False),
        }
        self.img = os.path.join(self.tmp.name, "d.img")
        synth.build_image(self.img, list(self.files.items()), 1200)
        self.session = os.path.join(self.tmp.name, "sess")
        cat.catalog(self.img, self.session, 0, 1200, on_progress=lambda p: None)
        self.dest = os.path.join(self.tmp.name, "out")
        self.plan = {"dest": self.dest, "kinds": ["jpeg", "video-mp4"], "include_suspect": False,
                     "batch": {"unit": "n", "size": 2}}

    def run_batch(self, plan=None, **kw):
        return ex.extract_batch(self.img, self.session, plan or self.plan, **kw)


class ExtractTest(ExtractBase):
    def test_batches_and_content(self):
        ov = ex.check_plan(self.session, self.plan)["overview"]
        self.assertEqual((ov["files"], ov["batches_remaining"]), (4, 2))  # 의심(moov 없음) 제외
        r = self.run_batch()
        self.assertEqual((r["state"], r["files"]), ("done", 2))
        r = self.run_batch()
        self.assertEqual((r["state"], r["files"]), ("done", 2))
        res = ex.check_plan(self.session, self.plan)
        self.assertFalse(res["ok"])
        self.assertIn("모두 꺼냈습니다", res["reasons"][0])
        # 원본 바이트와 정확히 같아야 한다
        done = ex.load_extracted(self.session)
        by_off = {rec["offset"] // BS: rec for rec in ex.load_catalog(self.session)}
        for blk, data in self.files.items():
            if blk == 900:
                continue
            rec = done[by_off[blk]["id"]]
            got = open(os.path.join(self.dest, rec["path"]), "rb").read()
            self.assertEqual(hashlib.sha256(got).hexdigest(), hashlib.sha256(data).hexdigest())
            self.assertEqual(rec["sha256"], hashlib.sha256(data).hexdigest())

    def test_layout_dates_use_local_time_for_video(self):
        self.run_batch(dict(self.plan, batch={"unit": "n", "size": 10}))
        paths = sorted(r["path"] for r in ex.load_extracted(self.session).values())
        days = sorted({p.split(os.sep)[0] for p in paths})
        # JPEG 2개(03-06, 03-07), 영상은 UTC 03-06 16:30 → KST 03-07, EXIF 없는 JPEG → 날짜불명
        self.assertEqual(days, ["2024-03-06", "2024-03-07", ex.UNDATED])
        self.assertTrue(any(p.startswith("2024-03-07") and p.endswith(".mp4") for p in paths))

    def test_suspect_only_when_requested(self):
        self.run_batch(dict(self.plan, include_suspect=True, batch={"unit": "n", "size": 10}))
        self.assertEqual(len(ex.load_extracted(self.session)), 5)  # 정상 4 + 의심 1(moov 없는 영상)

    def test_gb_batch_limit(self):
        recs = ex.select(ex.load_catalog(self.session), self.plan)
        b = ex.next_batch(recs, set(), {"unit": "gb", "size": 0.0005})  # 0.5MB
        self.assertGreaterEqual(len(b), 1)
        self.assertLess(len(b), len(recs))

    def test_resume_after_cancel(self):
        n = {"c": 0}

        def cancel():
            n["c"] += 1
            return n["c"] > 1
        r = self.run_batch(dict(self.plan, batch={"unit": "n", "size": 4}), should_cancel=cancel)
        self.assertEqual((r["state"], r["files"]), ("cancelled", 1))
        r = self.run_batch(dict(self.plan, batch={"unit": "n", "size": 4}))
        self.assertEqual((r["state"], r["files"]), ("done", 3))
        self.assertEqual(len(ex.load_extracted(self.session)), 4)

    def test_no_part_files_left_on_failure(self):
        real = ex.ReadOnlyDevice.read_at
        calls = {"n": 0}

        def flaky(self_, off, ln):
            calls["n"] += 1
            if calls["n"] == 2:  # 첫 파일은 성공, 두 번째 파일에서 장치가 사라짐
                raise OSError(6, "Device not configured")
            return real(self_, off, ln)
        with mock.patch.object(ex.ReadOnlyDevice, "read_at", flaky):
            r = self.run_batch()
        self.assertEqual((r["state"], r["files"]), ("disconnected", 1))
        leftovers = [f for _, _, fs in os.walk(self.dest) for f in fs if f.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_never_writes_to_source_image(self):
        before = hashlib.sha256(open(self.img, "rb").read()).hexdigest()
        self.run_batch(dict(self.plan, batch={"unit": "n", "size": 10}))
        self.assertEqual(hashlib.sha256(open(self.img, "rb").read()).hexdigest(), before)

    def test_refuses_bad_destinations(self):
        for bad in (os.path.join(guards.REPO, "recovered"), "/System/x", "/", ""):
            r = self.run_batch(dict(self.plan, dest=bad))
            self.assertEqual(r["state"], "refused", bad)
        r = self.run_batch(dict(self.plan, dest=os.path.join(self.tmp.name, "mnt", "x")),
                           forbidden=[os.path.join(self.tmp.name, "mnt")])
        self.assertEqual(r["state"], "refused")
        self.assertFalse(os.path.exists(os.path.join(guards.REPO, "recovered")))

    def test_refuses_when_not_enough_space(self):
        fake = mock.Mock(free=1000)
        with mock.patch.object(guards.shutil, "disk_usage", return_value=fake):
            res = ex.check_plan(self.session, self.plan)
        self.assertFalse(res["ok"])
        self.assertTrue(any("여유 공간이 부족" in x for x in res["reasons"]))


if __name__ == "__main__":
    unittest.main()
