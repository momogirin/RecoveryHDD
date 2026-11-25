"""끝부분 촘촘 검사: 기본 검사(샘플링)가 놓친 뒤쪽 파일의 흔적을 찾는다. 원본은 읽기만 한다."""
import hashlib
import os
import random
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from apfsrecover import server, tailcheck
from apfsrecover.blockdev import ReadOnlyDevice
from apfsrecover.server import App, Job
from . import synth

BLOCKS = 20000
LONE = 15003  # 기본 검사 범위 밖에 따로 떨어진 작은 사진


class TailCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = random.Random(7)
        self.front = [(2, synth.make_jpeg(r, 2_500_000)), (800, synth.make_mp4(r, 3_000_000, datetime(2024, 3, 6, 5, 0, 0))), (1700, synth.make_jpeg(r, 1_800_000))]
        self.lone = synth.make_jpeg(r, 30_000)
        self.patch = mock.patch.object(server, "MARGIN_AFTER_DATA", 0)  # 작은 가짜 디스크라 여유 2GB 를 빼야 뒤쪽이 남는다
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def app(self, with_lone, tail_step):
        img = os.path.join(self.tmp.name, "d%d.img" % with_lone)
        synth.build_image(img, self.front + ([(LONE, self.lone)] if with_lone else []), BLOCKS)
        a = App(os.path.join(self.tmp.name, "s%d_%d" % (with_lone, tail_step)), images={"demo": img},
                sample_step=1 << 20, tail_step=tail_step)
        dev = a.devices()[0]
        state, info = a._scan(Job("scan"), dev, False, False)
        self.assertEqual(state, "done")
        self.assertLess(info["end_bytes"], LONE * 4096)  # 기본 검사는 외딴 사진까지 가지 않는다
        return a, dev

    def run_tail(self, a, dev, resume=False):
        job = Job("tail")
        a.tail_target(dev, resume)(job)
        return job

    def test_empty_tail(self):
        a, dev = self.app(False, 16 * 1024)
        self.assertTrue(a.tail_info()["can"])
        job = self.run_tail(a, dev)
        self.assertEqual((job.state, job.result["verdict"], job.result["nonzero"]), ("done", "empty", 0))

    def test_lone_file_leaves_a_trace(self):
        a, dev = self.app(True, 16 * 1024)
        job = self.run_tail(a, dev)
        self.assertEqual(job.state, "done")
        self.assertGreater(job.result["nonzero"], 0)
        self.assertIn(job.result["verdict"], ("residue", "files"))
        st = tailcheck.load(a.session)
        self.assertTrue(st["finished"])
        self.assertTrue(any(LONE * 4096 <= off < (LONE * 4096 + len(self.lone)) for off in [st["last_data"]]))

    def test_every_block_finds_the_file_start(self):
        a, dev = self.app(True, 4096)
        job = self.run_tail(a, dev)
        self.assertEqual(job.result["verdict"], "files")
        self.assertEqual(tailcheck.load(a.session)["hits"], [{"offset": LONE * 4096, "kind": "jpg"}])
        self.assertIn("파일 시작 표시가 있음", a.report())

    def test_cancel_then_resume_matches(self):
        a, dev = self.app(True, 4096)
        info = a.tail_info()
        with ReadOnlyDevice(dev["path"]) as d:
            full = tailcheck.tail_check(d, os.path.join(self.tmp.name, "x"), info["scanned_bytes"], info["size"], 4096)
        calls = {"n": 0}

        def soon():
            calls["n"] += 1
            return calls["n"] > 700

        with ReadOnlyDevice(dev["path"]) as d:
            st = tailcheck.tail_check(d, a.session, info["scanned_bytes"], info["size"], 4096, should_cancel=soon)
        self.assertFalse(st["finished"])
        self.assertEqual(tailcheck.verdict(st), "partial")
        with ReadOnlyDevice(dev["path"]) as d:
            st = tailcheck.tail_check(d, a.session, info["scanned_bytes"], info["size"], 4096, resume=True)
        for k in ("samples", "nonzero", "hits", "last_data", "finished"):
            self.assertEqual(st[k], full[k], k)

    def test_original_untouched_and_needs_finished_scan(self):
        a, dev = self.app(True, 16 * 1024)
        before = hashlib.sha256(open(dev["path"], "rb").read()).hexdigest()
        self.run_tail(a, dev)
        self.assertEqual(hashlib.sha256(open(dev["path"], "rb").read()).hexdigest(), before)
        b = App(os.path.join(self.tmp.name, "empty"), images={"demo": dev["path"]})
        self.assertFalse(b.tail_info()["can"])                                         # 검사 전에는 할 수 없다


if __name__ == "__main__":
    unittest.main()
