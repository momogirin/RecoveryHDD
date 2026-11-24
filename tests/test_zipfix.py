"""목차가 없거나 끝이 잘린 ZIP 에서 온전한 항목만 모아 복원본을 만든다."""
import io
import os
import random
import tempfile
import unittest
import zipfile

from apfsrecover import verify, zipfix
from .test_zip import make_zip


def names_and_data(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {i.filename: z.read(i) for i in z.infolist()}


class ZipFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = random.Random(5)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def good(self, n=6, **kw):
        data = make_zip(self.r, n, **kw)
        return data, self.write("full.zip", data)

    def test_missing_directory_recovers_all_entries(self):
        for kw in ({}, {"method": zipfile.ZIP_STORED}, {"stream": True}):
            full, fp = self.good(**kw)
            want = names_and_data(fp)
            cut = self.write("cut.zip", full[:full.index(b"PK\x01\x02")])          # 목차를 떼어냄
            with self.assertRaises(zipfile.BadZipFile):
                zipfile.ZipFile(cut)
            out = os.path.join(self.tmp.name, "fix", "r.zip")
            sv = zipfix.salvage(cut, out)
            self.assertEqual((sv["entries"], sv["lost"], sv["stopped"]), (6, 0, "end"), kw)
            self.assertEqual(names_and_data(out), want, kw)                          # 내용이 원본 항목과 같다

    def test_cut_in_last_entry_keeps_the_rest(self):
        full, fp = self.good(8)
        want = names_and_data(fp)
        with zipfile.ZipFile(fp) as z:
            last = z.infolist()[-1].header_offset
        cut = self.write("cut.zip", full[:last + 1000])                              # 마지막 항목 중간에서 잘림
        out = os.path.join(self.tmp.name, "r.zip")
        sv = zipfix.salvage(cut, out)
        self.assertEqual((sv["entries"], sv["lost"], sv["stopped"]), (7, 1, "damaged"))
        got = names_and_data(out)
        self.assertEqual(got, {k: v for k, v in list(want.items())[:7]})

    def test_corrupt_middle_entry_stops_there(self):
        full, fp = self.good(6)
        with zipfile.ZipFile(fp) as z:
            infos = z.infolist()
        bad = bytearray(full)
        bad[infos[3].header_offset + 200] ^= 0xFF                                     # 네 번째 항목 데이터 손상
        cut = self.write("bad.zip", bytes(bad))
        sv = zipfix.salvage(cut, os.path.join(self.tmp.name, "r.zip"))
        self.assertEqual((sv["entries"], sv["lost"]), (3, 1))

    def test_nothing_recoverable_and_valid_zip(self):
        self.assertIsNone(zipfix.salvage(self.write("junk.zip", b"PK\x03\x04" + b"\0" * 500), os.path.join(self.tmp.name, "r.zip")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "r.zip")))
        full, fp = self.good(3)                                                      # 온전한 ZIP 은 목차를 만나 그대로 같은 항목
        sv = zipfix.salvage(fp, os.path.join(self.tmp.name, "same.zip"))
        self.assertEqual((sv["entries"], sv["stopped"]), (3, "central"))

    def test_verify_session_creates_fixed_copy(self):
        """열림 확인 단계에서 목차 없는 ZIP 은 fixed 로 표시하고 _복원본 폴더에 새 파일을 만든다."""
        import json
        full, _ = self.good(5)
        cut = full[:full.index(b"PK\x01\x02")]
        dest = os.path.join(self.tmp.name, "out")
        rel = os.path.join("압축", "_의심", "날짜불명", "zip_000001_blk4.zip")
        os.makedirs(os.path.dirname(os.path.join(dest, rel)))
        with open(os.path.join(dest, rel), "wb") as f:
            f.write(cut)
        sess = os.path.join(self.tmp.name, "s")
        os.makedirs(sess)
        with open(os.path.join(sess, "extracted.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": 1, "path": rel, "bytes": len(cut), "kind": "zip"}) + "\n")
        with open(os.path.join(sess, "catalog.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": 1, "offset": 4096, "length": len(cut), "kind": "zip", "structure": "suspect"}) + "\n")
        summary = verify.verify_session(sess, dest)
        self.assertEqual((summary["ok"], summary["fail"], summary["fixed"], summary["unchecked"]), (0, 0, 1, 0))
        fx = verify.load_fixed(sess)
        self.assertEqual(len(fx), 1)
        self.assertEqual(fx[0]["salvage"]["entries"], 5)
        self.assertTrue(os.path.isfile(os.path.join(dest, fx[0]["salvage"]["path"])))
        self.assertEqual(open(os.path.join(dest, rel), "rb").read(), cut)            # 원본은 그대로


if __name__ == "__main__":
    unittest.main()
