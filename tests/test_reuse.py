"""이전 검사 결과 재사용: 이미 찾은 구간은 디스크에서 다시 읽지 않고, 새 종류는 나머지에서 찾는다."""
import os
import random
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from apfsrecover import catalog as cat
from apfsrecover.blockdev import ReadOnlyDevice
from apfsrecover.server import App, Job
from . import synth
from .test_formats_more import make_pdf


class CountingDevice(ReadOnlyDevice):
    total = 0

    def read_at(self, offset, length):
        data = super().read_at(offset, length)
        CountingDevice.total += len(data)
        return data


def recs(session):
    return [(r["offset"], r["length"], r["kind"], r["structure"]) for r in cat.read_jsonl(os.path.join(session, "catalog.jsonl"))]


class ReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = random.Random(3)
        self.old_files = [(2, synth.make_jpeg(r, 2_500_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))),
                          (800, synth.make_mp4(r, 3_000_000, datetime(2024, 3, 6, 5, 0, 0))),
                          (1700, synth.make_jpeg(r, 1_800_000))]
        self.pdf = make_pdf(r, "plain")
        self.img = os.path.join(self.tmp.name, "demo.img")
        synth.build_image(self.img, self.old_files, 2400)          # 이전 검사 때의 디스크(문서가 아직 없는 버전)
        self.old = App(os.path.join(self.tmp.name, "session_old"), images={"demo": self.img}, sample_step=64 * 1024)
        self.assertEqual(self.old._scan(Job("scan"), self.old.devices()[0], False, False)[0], "done")
        synth.build_image(self.img, self.old_files + [(1650, self.pdf)], 2400)   # 같은 디스크에 새 종류(PDF)가 있다
        self.new = App(os.path.join(self.tmp.name, "session"), images={"demo": self.img}, sample_step=64 * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        CountingDevice.total = 0
        with mock.patch.object(cat, "ReadOnlyDevice", CountingDevice):
            state = self.new._scan(Job("scan"), self.new.devices()[0], False, False, **kw)[0]
        return state, CountingDevice.total

    def test_reusable_lists_same_disk_only(self):
        dev = self.new.devices()[0]
        self.assertEqual([c["name"] for c in self.new.reusable(dev)], ["session_old"])
        other = os.path.join(self.tmp.name, "other.img")
        synth.build_image(other, [(5, b"x" * 4096)], 2400)
        app = App(os.path.join(self.tmp.name, "s2"), images={"other": other}, sample_step=64 * 1024)
        self.assertEqual(app.reusable(app.devices()[0]), [])               # 다른 디스크의 기록은 쓰지 않는다

    def test_reuse_gives_same_result_but_reads_much_less(self):
        full_state, full_read = self.scan()                                  # 처음부터 읽기
        full = recs(self.new.session)
        self.assertEqual(full_state, "done")
        self.new = App(os.path.join(self.tmp.name, "session2"), images={"demo": self.img}, sample_step=64 * 1024)
        dev = self.new.devices()[0]
        old_path = next(c["path"] for c in self.new.reusable(dev) if c["name"] == "session_old")
        state, read = self.scan(reuse=old_path)
        self.assertEqual(state, "done")
        self.assertEqual(recs(self.new.session), full)                       # 결과는 같고
        self.assertIn("pdf", [k for _, _, k, _ in full])                     # 새 종류(PDF)도 찾았고
        self.assertLess(read, full_read * 0.4)                               # 읽은 양은 크게 줄었다
        got = cat.read_jsonl(os.path.join(self.new.session, "catalog.jsonl"))
        self.assertEqual(sum(1 for r in got if r.get("reused")), 3)          # 옛 기록에서 가져온 것 표시
        self.assertEqual([r["id"] for r in got], list(range(1, len(got) + 1)))

    def test_resume_after_cancel_with_known_matches_uninterrupted(self):
        self.scan()
        full = recs(self.new.session)
        known = [r for r in cat.read_jsonl(os.path.join(self.old.session, "catalog.jsonl"))]
        sess = os.path.join(self.tmp.name, "resume")
        calls = {"n": 0}

        def cancel_soon():
            calls["n"] += 1
            return calls["n"] > 3

        self.assertEqual(cat.catalog(self.img, sess, 0, 2400, fresh=True, known=known, should_cancel=cancel_soon), 3)
        self.assertNotEqual(recs(sess), full)                                # 중간에 멈췄다
        self.assertEqual(cat.catalog(self.img, sess, 0, 2400, fresh=False, known=known), 0)   # 이어하기
        self.assertEqual(recs(sess), full)                                   # 중복·누락 없이 같은 결과

    def test_bad_known_entries_are_ignored(self):
        known = [{"id": 1, "offset": 4096 * 2, "length": 100 * 1024 * 1024 * 1024, "kind": "jpeg", "structure": "ok"},   # 범위를 넘음
                 {"id": 2, "offset": 4097, "length": 10, "kind": "jpeg", "structure": "ok"},                            # 정렬 안 됨
                 {"id": 3, "offset": 4096 * 5, "length": 0, "kind": "jpeg", "structure": "ok"}]                         # 길이 0
        sess = os.path.join(self.tmp.name, "bad")
        self.assertEqual(cat.catalog(self.img, sess, 0, 2400, fresh=True, known=known), 0)
        self.assertFalse(any(r.get("reused") for r in cat.read_jsonl(os.path.join(sess, "catalog.jsonl"))))


if __name__ == "__main__":
    unittest.main()
