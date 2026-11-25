"""도구 4가지: 원래 이름·폴더(APFS 목록), 파티션 찾기, 디스크 백업(이미지), 건강 상태."""
import hashlib
import json
import os
import random
import struct
import tempfile
import unittest
from types import SimpleNamespace

from apfsrecover import apfsmeta, extract as ex, health, imaging, partfind
from apfsrecover.server import App, Job
from . import apfs_synth as A
from . import synth

BS = 4096


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def wb(path, data):
    with open(path, "wb") as f:
        f.write(data)


def reader(path):
    f = open(path, "rb")

    def read_at(off, n):
        f.seek(off)
        return f.read(n)
    return read_at, f


class ApfsNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = [("Photos/2024/IMG_0001.JPG", 40, 300000), ("Photos/IMG_0002.HEIC", 120, 9000), ("문서/보고서.pdf", 200, 5000)]

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, blocks, base=0, total=400):
        p = os.path.join(self.tmp.name, "c.img")
        with open(p, "wb") as f:
            f.truncate(base + total * BS)
            for n, b in blocks.items():
                f.seek(base + n * BS)
                f.write(b)
        ra, f = reader(p)
        try:
            return apfsmeta.read_names(ra, base)
        finally:
            f.close()

    def test_single_and_two_level_trees(self):
        for two in (False, True):
            for hashed in (True, False):
                r = self.names(A.build(self.files, 400, two_level=two, hashed=hashed))
                self.assertTrue(r["ok"], r)
                self.assertEqual(r["by_block"], {40: "Macintosh HD/Photos/2024/IMG_0001.JPG", 120: "Macintosh HD/Photos/IMG_0002.HEIC",
                                                 200: "Macintosh HD/문서/보고서.pdf"}, (two, hashed))

    def test_newer_checkpoint_damaged_uses_copy_and_block0_lost(self):
        blocks = A.build(self.files, 400)
        b0 = bytearray(blocks[0]); b0[200] ^= 0xFF; blocks[0] = bytes(b0)          # 블록 0 체크섬 깨짐 → 체크포인트 사본(블록 1)
        self.assertTrue(self.names(blocks)["ok"])
        blocks[0] = b"\0" * BS                                                        # 블록 0 이 아예 비어도 앞쪽을 훑어 찾는다
        self.assertTrue(self.names(blocks)["ok"])

    def test_container_not_at_start(self):
        self.assertTrue(self.names(A.build(self.files, 400), base=1 << 20)["ok"])

    def test_damaged_tree_and_encrypted(self):
        blocks = A.build(self.files, 400, two_level=True)
        b = bytearray(blocks[9]); b[300] ^= 0xFF; blocks[9] = bytes(b)               # 잎 노드 하나 손상: 나머지는 찾는다
        r = self.names(blocks)
        self.assertTrue(r["ok"])
        self.assertGreater(r["bad_blocks"], 0)
        self.assertLess(len(r["by_block"]), 3)
        r = self.names(A.build(self.files, 400, encrypted=True))
        self.assertEqual(r["volumes"][0]["encrypted"], True)
        self.assertEqual(r["by_block"], {})
        r = self.names({})
        self.assertFalse(r["ok"])
        self.assertIn("찾지 못했어요", r["error"])


class PartFindTest(unittest.TestCase):
    def test_lost_apfs_and_mbr(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d.img")
            blocks = A.build([], 300)
            with open(p, "wb") as f:
                f.truncate(8 << 20)
                mbr = bytearray(512)
                mbr[446 + 4] = 0x07
                struct.pack_into("<II", mbr, 446 + 8, 2048, 4096)                     # 1MB 에 NTFS 라고 적힌 표(실제 내용은 없음)
                mbr[510:512] = b"\x55\xaa"
                f.seek(0); f.write(bytes(mbr))
                f.seek(3 << 20); f.write(blocks[0])                                    # 표에 없는 3MB 위치의 APFS
            ra, f = reader(p)
            try:
                r = partfind.find(ra, 8 << 20)
            finally:
                f.close()
        self.assertEqual(r["table"], "MBR")
        self.assertTrue(r["finished"])
        by = {x["offset"]: x for x in r["parts"]}
        self.assertTrue(by[1 << 20]["in_table"])
        self.assertIsNone(by[1 << 20]["fs"])
        self.assertEqual((by[3 << 20]["fs"], by[3 << 20]["in_table"], by[3 << 20]["ok"]), ("APFS", False, True))


class FlakyDevice:
    """특정 구간은 읽을 때 오류가 나는 가짜 장치."""
    def __init__(self, path, bad=()):
        self.f = open(path, "rb")
        self.bad = bad

    def read_at(self, off, n):
        for s, e in self.bad:
            if off < e and s < off + n:
                raise OSError(5, "I/O error")
        self.f.seek(off)
        return self.f.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


class BackupTest(unittest.TestCase):
    def test_copy_skips_bad_and_resumes(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "src.img")
            data = os.urandom(20 << 20)
            wb(src, data)
            img = os.path.join(t, "out", "b.img")
            bad = [(9 << 20, (9 << 20) + 100)]
            st = imaging.backup(src, img, 0, len(data), dev_factory=lambda p: FlakyDevice(p, bad))
            self.assertTrue(st["finished"])
            self.assertEqual(st["bad"], [[9 << 20, 64 << 10]])                     # 못 읽은 64KB 만 0 으로
            out = rb(img)
            self.assertEqual(out[:9 << 20], data[:9 << 20])
            self.assertEqual(out[9 << 20:(9 << 20) + (64 << 10)], b"\0" * (64 << 10))
            self.assertEqual(out[(9 << 20) + (64 << 10):], data[(9 << 20) + (64 << 10):])
            # 멈췄다가 이어서 해도 같은 결과
            img2 = os.path.join(t, "c.img")
            calls = {"n": 0}

            def soon():
                calls["n"] += 1
                return calls["n"] > 1
            st = imaging.backup(src, img2, 0, len(data), should_cancel=soon, dev_factory=lambda p: FlakyDevice(p))
            self.assertFalse(st["finished"])
            st = imaging.backup(src, img2, 0, len(data), resume=True, dev_factory=lambda p: FlakyDevice(p))
            self.assertTrue(st["finished"])
            self.assertEqual(hashlib.sha256(rb(img2)).digest(), hashlib.sha256(data).digest())


class HealthTest(unittest.TestCase):
    def run_with(self, out, rc=0):
        return lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out.encode())

    def test_windows(self):
        dev = {"id": "disk2p2", "whole_disk": "disk2", "content": "Apple_APFS"}
        r = health.health(dev, self.run_with(json.dumps({"Health": "Healthy", "Temp": 38, "Hours": 1200, "ReadUnc": 0})), "win32")
        self.assertEqual(r["status"], "ok")
        self.assertIn(["온도", "38℃"], r["items"])
        r = health.health(dev, self.run_with(json.dumps({"Health": "Healthy", "ReadUnc": 3})), "win32")
        self.assertEqual(r["status"], "warn")
        r = health.health(dev, self.run_with(json.dumps({"Health": "Healthy"})), "win32")
        self.assertIn("막고", r["note"])                                              # USB 가 SMART 를 막은 경우
        self.assertFalse(health.health(dev, self.run_with("{}"), "win32")["available"])
        self.assertFalse(health.health({"content": "disk-image"})["available"])

    def test_mac(self):
        dev = {"id": "disk4s2", "whole_disk": "disk4", "content": "Apple_APFS"}
        self.assertEqual(health.health(dev, self.run_with("   SMART Status:              Verified\n"), "darwin")["status"], "ok")
        self.assertEqual(health.health(dev, self.run_with("   SMART Status:              Not Supported\n"), "darwin")["status"], "unknown")


class NamesEndToEndTest(unittest.TestCase):
    def test_extracted_files_get_original_names_as_links(self):
        with tempfile.TemporaryDirectory() as t:
            r = random.Random(2)
            jpg = synth.make_jpeg(r, 200_000)
            blocks = A.build([("사진/여행/IMG_0001.JPG", 40, len(jpg))], 300)
            img = os.path.join(t, "apfs.img")
            A.write_image(img, blocks, 300, [(40, jpg)])
            app = App(os.path.join(t, "rec", "session"), images={"mac": img}, sample_step=64 * 1024)
            dev = app.find_device("image:mac")
            self.assertEqual(app._scan(Job("scan"), dev, False, False)[0], "done")
            dest = os.path.join(t, "out")
            plan = {"dest": dest, "kinds": ["jpeg"], "include_suspect": True, "batch": {"unit": "n", "size": 10 ** 9}}
            self.assertEqual(ex.extract_batch(dev["path"], app.session, plan)["files"], 1)
            before = rb(img)
            job = Job("names")
            app.names_target(dev)(job)
            self.assertEqual(job.state, "done", job.error)
            self.assertEqual((job.result["matched"], job.result["linked"]), (1, 1))
            linked = os.path.join(dest, App.NAMES_DIR, "Macintosh HD", "사진", "여행", "IMG_0001.JPG")
            self.assertEqual(rb(linked), jpg)
            self.assertTrue(os.path.isfile(os.path.join(dest, App.NAMES_DIR, "목록.csv")))
            self.assertEqual(rb(img), before)                         # 원본은 그대로


if __name__ == "__main__":
    unittest.main()
