import os
import random
import tempfile
import time
import unittest
from datetime import datetime

from apfsrecover import catalog as cat
from apfsrecover import extract as ex
from apfsrecover import verify as vf
from . import synth

NO_TOOLS = {"pillow": False, "sips": False, "ffprobe": False}


class VerifyTest(unittest.TestCase):
    def setUp(self):
        if hasattr(time, "tzset"):  # 윈도우는 TZ 환경변수를 이 형식으로 이해하지 못해 시간대가 흔들린다
            os.environ["TZ"] = "Asia/Seoul"
            time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = random.Random(3)
        self.d = self.tmp.name

    def write(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_good_files_pass_structure(self):
        j = synth.make_jpeg(self.r, 50_000)
        m = synth.make_mp4(self.r, 100_000, datetime(2024, 1, 1))
        c = synth.make_cr3(self.r, 100_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:01:01 00:00:00"),
                           synth.make_tiff("Canon", "Canon EOS R8", "2024:01:01 00:00:00"))
        for kind, data in (("jpeg", j), ("video-mp4", m), ("raw-cr3", c)):
            r = vf.verify_file(self.write(kind, data), kind, len(data), NO_TOOLS)
            self.assertEqual((r["status"], r["level"]), ("ok", "structure"), kind)

    def test_truncated_and_corrupt_files_fail(self):
        j = synth.make_jpeg(self.r, 50_000)
        m = synth.make_mp4(self.r, 100_000, datetime(2024, 1, 1))
        cases = [("jpeg", j[:-1000], "잘림"), ("jpeg", j[:-2], "잘림"),
                 ("video-mp4", m[:-5000], "박스"), ("video-mp4", m + b"\0" * 20, "박스")]
        for i, (kind, data, word) in enumerate(cases):
            r = vf.verify_file(self.write("f%d" % i, data), kind, len(data), NO_TOOLS)
            self.assertEqual(r["status"], "fail", (kind, word))
            self.assertIn(word, r["reason"])

    def test_deep_decode_catches_structurally_valid_but_undecodable_jpeg(self):
        tools = vf.available_tools()
        if not (tools["pillow"] or tools["sips"]):
            self.skipTest("디코더(Pillow/sips) 없음")
        j = synth.make_jpeg(self.r, 50_000)  # 구조는 맞지만 내용은 무작위
        p = self.write("fake.jpg", j)
        self.assertEqual(vf.verify_file(p, "jpeg", len(j), tools, deep=False)["status"], "ok")
        r = vf.verify_file(p, "jpeg", len(j), tools, deep=True)
        self.assertEqual((r["status"], r["level"]), ("fail", "decode"))

    def test_missing_and_size_mismatch(self):
        j = synth.make_jpeg(self.r, 5000)
        self.assertIn("없음", vf.verify_file(os.path.join(self.d, "nope"), "jpeg", 10, NO_TOOLS)["reason"])
        p = self.write("a", j)
        self.assertIn("크기 불일치", vf.verify_file(p, "jpeg", len(j) + 1, NO_TOOLS)["reason"])

    def test_end_to_end_with_tampered_file(self):
        r = random.Random(5)
        files = {1: synth.make_jpeg(r, 300_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35")),
                 100: synth.make_mp4(r, 800_000, datetime(2024, 3, 6, 1, 0, 0))}
        img = os.path.join(self.d, "d.img")
        synth.build_image(img, list(files.items()), 700)
        sess = os.path.join(self.d, "sess")
        dest = os.path.join(self.d, "out")
        cat.catalog(img, sess, 0, 700, on_progress=lambda p: None)
        plan = {"dest": dest, "kinds": ["jpeg", "video-mp4"], "batch": {"unit": "n", "size": 10}}
        self.assertEqual(ex.extract_batch(img, sess, plan)["state"], "done")
        # 합성 JPEG 는 구조만 맞고 내용은 무작위라서, 구조 검증(deep=False)으로 확인한다
        s = vf.verify_session(sess, dest, deep=False)
        self.assertEqual((s["total"], s["ok"], s["fail"]), (2, 2, 0))
        # 꺼낸 영상 끝을 잘라서(크기가 바뀌게) 검증이 실패를 잡는지
        done = ex.load_extracted(sess)
        mp4 = [x for x in done.values() if x["kind"] == "video-mp4"][0]
        p = os.path.join(dest, mp4["path"])
        with open(p, "r+b") as f:
            f.truncate(os.path.getsize(p) - 1000)
        s = vf.verify_session(sess, dest, deep=False)
        self.assertEqual((s["ok"], s["fail"]), (1, 1))
        fails = vf.load_failures(sess)
        self.assertEqual(len(fails), 1)
        self.assertIn("크기 불일치", fails[0]["reason"])


if __name__ == "__main__":
    unittest.main()
