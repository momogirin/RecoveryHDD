import json
import os
import random
import tempfile
import unittest
from datetime import datetime

from apfsrecover import catalog as cat
from apfsrecover.exif import parse_tiff, to_iso
from apfsrecover.formats import jpeg_parse
from . import synth
from .synth import BS


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rng = random.Random(1234)
        r = self.rng
        tiff = synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35")
        self.jpg = synth.make_jpeg(r, 300_000, tiff)
        self.jpg_noexif = synth.make_jpeg(r, 120_000)
        self.jpg_trunc = synth.make_jpeg(r, 200_000, eoi=False)
        self.jpg_small = synth.make_jpeg(r, 2_000)
        self.mp4 = synth.make_mp4(r, 1_500_000, datetime(2024, 1, 2, 3, 4, 5))
        self.mp4_nomoov = synth.make_mp4(r, 600_000, datetime(2024, 1, 2), moov=False)
        self.cr3 = synth.make_cr3(r, 900_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:05:06 07:08:09"),
                                  synth.make_tiff("Canon", "ignored-in-cmt2", "2024:05:06 07:08:09"))
        decoy = synth.box(b"ftyp", b"mp42") + b"\xde\xad" * 100000  # ftyp 뒤가 박스가 아님
        items = [(1, self.jpg), (200, self.jpg_noexif), (400, self.jpg_trunc), (600, self.jpg_small),
                 (700, self.mp4), (1200, self.mp4_nomoov), (1500, self.cr3), (2000, decoy)]
        self.items = dict(items)
        self.img = os.path.join(self.tmp.name, "disk.img")
        synth.build_image(self.img, items, 2600)
        self.total = 2600

    def run_catalog(self, session=None, **kw):
        session = session or os.path.join(self.tmp.name, "session")
        rc = cat.catalog(self.img, session, 0, self.total, on_progress=lambda p: None, **kw)
        return rc, session

    @staticmethod
    def records(session):
        return [json.loads(l) for l in open(os.path.join(session, "catalog.jsonl"))]


class CatalogTest(Base):
    def test_finds_expected_files_exactly(self):
        rc, s = self.run_catalog()
        self.assertEqual(rc, 0)
        recs = {r["offset"] // BS: r for r in self.records(s)}
        self.assertEqual(sorted(recs), [1, 200, 700, 1200, 1500])  # 잘린 JPEG·작은 JPEG·가짜 ftyp 제외
        for blk, r in recs.items():
            self.assertEqual(r["length"], len(self.items[blk]), "블록 %d 길이" % blk)

    def test_metadata(self):
        _, s = self.run_catalog()
        recs = {r["offset"] // BS: r for r in self.records(s)}
        a = recs[1]
        self.assertEqual((a["kind"], a["structure"], a["model"], a["taken"], a["width"], a["height"]),
                         ("jpeg", "ok", "Canon EOS R8", "2024-03-06T09:38:35", 640, 480))
        self.assertIsNone(recs[200]["taken"])
        self.assertEqual((recs[700]["kind"], recs[700]["structure"], recs[700]["taken"]),
                         ("video-mp4", "ok", "2024-01-02T03:04:05"))
        self.assertEqual((recs[1200]["kind"], recs[1200]["structure"]), ("video-mp4", "suspect"))
        self.assertEqual((recs[1500]["kind"], recs[1500]["structure"], recs[1500]["model"], recs[1500]["taken"]),
                         ("raw-cr3", "ok", "Canon EOS R8", "2024-05-06T07:08:09"))

    def test_resume_gives_same_result(self):
        cat.CHUNK = 64 * BS
        self.addCleanup(setattr, cat, "CHUNK", 8 * 1024 * 1024)
        _, full = self.run_catalog(session=os.path.join(self.tmp.name, "full"))
        sess = os.path.join(self.tmp.name, "resume")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 5
        rc, _ = self.run_catalog(session=sess, should_cancel=cancel)
        self.assertEqual(rc, 3)
        st = json.load(open(os.path.join(sess, "progress.json")))
        self.assertEqual(st["state"], "cancelled")
        self.assertLess(st["next_pos"], self.total * BS)
        rc, _ = self.run_catalog(session=sess)
        self.assertEqual(rc, 0)
        strip = lambda rs: [(r["offset"], r["length"], r["kind"], r["structure"]) for r in rs]
        self.assertEqual(strip(self.records(sess)), strip(self.records(full)))

    def test_rescan_with_other_range_archives_old_session(self):
        rc, s = self.run_catalog()
        n_before = len(self.records(s))
        open(os.path.join(s, "extracted.jsonl"), "w").write('{"id": 1}\n')
        rc = cat.catalog(self.img, s, 0, 1000, on_progress=lambda p: None)  # 다른 범위
        self.assertEqual(rc, 0)
        archives = [d for d in os.listdir(s) if d.startswith("archive_")]
        self.assertEqual(len(archives), 1)
        self.assertEqual(len(self.records(os.path.join(s, archives[0]))), n_before)
        self.assertTrue(os.path.exists(os.path.join(s, archives[0], "extracted.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(s, "extracted.jsonl")))
        ids = [r["id"] for r in self.records(s)]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))  # 번호가 새로 1부터

    def test_fresh_flag_forces_new_scan(self):
        rc, s = self.run_catalog()
        rc = cat.catalog(self.img, s, 0, self.total, on_progress=lambda p: None, fresh=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len([d for d in os.listdir(s) if d.startswith("archive_")]), 1)

    def test_summary(self):
        _, s = self.run_catalog()
        sm = cat.summarize(s)
        self.assertEqual(sm["total"], 5)
        self.assertEqual(sm["kinds"], {"jpeg": 2, "video-mp4": 2, "raw-cr3": 1})
        self.assertEqual(sm["undated"], 2)  # EXIF 없는 JPEG, moov 없는 MP4
        self.assertEqual(sm["models"], {"Canon EOS R8": 2})

    def test_session_inside_repo_rejected(self):
        with self.assertRaises(ValueError):
            cat.catalog(self.img, os.path.join(cat.REPO, "oops_session"), 0, 10)
        self.assertFalse(os.path.exists(os.path.join(cat.REPO, "oops_session")))


class ParserTest(unittest.TestCase):
    def test_exif(self):
        t = parse_tiff(synth.make_tiff("Canon", "EOS R8 test", "2024:03:06 09:38:35"))
        self.assertEqual((t["make"], t["model"], t["taken"]), ("Canon", "EOS R8 test", "2024-03-06T09:38:35"))

    def test_bad_dates(self):
        self.assertIsNone(to_iso(b"0000:00:00 00:00:00"))
        self.assertIsNone(to_iso(b"1970:01:01 00:00:00"))
        self.assertIsNone(to_iso(b"garbage"))

    def test_jpeg_statuses(self):
        rng = random.Random(1)
        full = synth.make_jpeg(rng, 5000)
        self.assertEqual(jpeg_parse(full)[0:2], ("ok", len(full)))
        self.assertEqual(jpeg_parse(full[:len(full) // 2])[0], "short")
        self.assertEqual(jpeg_parse(b"\x00" * 100)[0], "bad")
        self.assertEqual(jpeg_parse(b"\xff\xd8\xff\x00junk")[0], "bad")
        trunc = synth.make_jpeg(rng, 5000, eoi=False)
        self.assertEqual(jpeg_parse(trunc + b"\0" * 8192 + b"\xff\xc4junk")[0], "bad")  # 잘림 + 0 + 다른 데이터
        self.assertEqual(jpeg_parse(trunc + b"\xff\xd8\xff\xe0" + b"x" * 50)[0], "bad")  # 잘림 + 다른 JPEG
        self.assertEqual(jpeg_parse(full[:-2])[0], "short")  # EOI 만 빠진 경우는 판단 불가


if __name__ == "__main__":
    unittest.main()


class CrashRecoveryTest(Base):
    def test_resume_after_crash_leaves_no_duplicates_or_broken_lines(self):
        cat.CHUNK = 64 * BS
        self.addCleanup(setattr, cat, "CHUNK", 8 * 1024 * 1024)
        _, full = self.run_catalog(session=os.path.join(self.tmp.name, "full"))
        want = [(r["offset"], r["length"]) for r in self.records(full)]
        sess = os.path.join(self.tmp.name, "crash")
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 12  # 중간까지 진행
        self.run_catalog(session=sess, should_cancel=cancel)
        st = json.load(open(os.path.join(sess, "progress.json")))
        # 갑자기 꺼진 상황 흉내: (1) 진행 기록보다 뒤의 항목이 이미 목록에 적혀 있음 (2) 마지막 줄이 잘림
        later = [r for r in [json.loads(l) for l in open(os.path.join(full, "catalog.jsonl"))]
                 if r["offset"] >= st["next_pos"]]
        with open(os.path.join(sess, "catalog.jsonl"), "a") as f:
            if later:
                f.write(json.dumps(later[0]) + "\n")
            f.write('{"id": 99, "kind": "jpe')  # 잘린 줄
        rc, _ = self.run_catalog(session=sess)
        self.assertEqual(rc, 0)
        got = [(r["offset"], r["length"]) for r in self.records(sess)]
        self.assertEqual(got, want)
        ids = [r["id"] for r in self.records(sess)]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))


class ThumbTest(unittest.TestCase):
    """검사 중 내장 미리보기 저장(07-roadmap B). 디스크를 더 읽지 않고 이미 읽은 바이트에서 꺼낸다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = random.Random(77)
        self.t_jpg = synth.make_thumb(r)
        self.t_cr3 = synth.make_thumb(r, 2500)
        items = [
            (1, synth.make_jpeg(r, 300_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35",
                                                           thumb=self.t_jpg))),
            (200, synth.make_jpeg(r, 120_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:40:00"))),  # IFD1 없음
            (400, synth.make_jpeg(r, 120_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:41:00",
                                                             thumb=self.t_jpg, thumb_off_delta=7))),   # 위치가 틀림
            (600, synth.make_jpeg(r, 120_000)),                                                    # EXIF 없음
            (800, synth.make_cr3(r, 900_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:05:06 07:08:09"),
                                 synth.make_tiff("Canon", "ignored-in-cmt2", "2024:05:06 07:08:09"), thumb=self.t_cr3)),
            (1100, synth.make_mp4(r, 400_000, datetime(2024, 1, 2))),
        ]
        self.img = os.path.join(self.tmp.name, "disk.img")
        synth.build_image(self.img, items, 1400)
        self.session = os.path.join(self.tmp.name, "session")

    def run_catalog(self, **kw):
        return cat.catalog(self.img, self.session, 0, 1400, on_progress=lambda p: None, **kw)

    def by_block(self):
        return {r["offset"] // BS: r for r in cat.read_jsonl(os.path.join(self.session, "catalog.jsonl"))}

    def thumb(self, rid):
        with open(cat.thumb_path(self.session, rid), "rb") as f:
            return f.read()

    def test_saves_embedded_thumbs_only_when_valid(self):
        self.assertEqual(self.run_catalog(), 0)
        recs = self.by_block()
        self.assertEqual(sorted(recs), [1, 200, 400, 600, 800, 1100])
        with_thumb = sorted(b for b, r in recs.items() if r.get("thumb"))
        self.assertEqual(with_thumb, [1, 800])
        self.assertEqual(self.thumb(recs[1]["id"]), self.t_jpg)
        self.assertEqual(self.thumb(recs[800]["id"]), self.t_cr3)
        files = sorted(os.listdir(os.path.join(self.session, cat.THUMBS)))
        self.assertEqual(files, sorted("%d.jpg" % recs[b]["id"] for b in with_thumb))  # 미리보기 없는 항목은 파일도 없다
        self.assertNotIn("_thumb", json.dumps(list(recs.values())))  # 바이트가 기록에 섞이지 않는다
        self.assertEqual(recs[1]["taken"], "2024-03-06T09:38:35")  # 다른 정보는 그대로

    def test_rescan_archives_old_thumbs(self):
        self.run_catalog()
        self.run_catalog(fresh=True)
        archives = [d for d in os.listdir(self.session) if d.startswith("archive_")]
        self.assertEqual(len(archives), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.session, archives[0], cat.THUMBS))), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.session, cat.THUMBS))), 2)

    def test_resume_drops_thumbs_of_discarded_records(self):
        cat.CHUNK = 64 * BS  # 작은 디스크라도 여러 번 나눠 읽게 해서 중간에 멈출 수 있게 한다
        self.addCleanup(setattr, cat, "CHUNK", 8 * 1024 * 1024)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2

        self.assertEqual(self.run_catalog(should_cancel=cancel), 3)
        stray = cat.thumb_path(self.session, 50)  # 이어하기 때 다시 붙을 번호의 옛 미리보기(갑자기 꺼진 경우)
        with open(stray, "wb") as f:
            f.write(b"old")
        self.assertEqual(self.run_catalog(), 0)
        self.assertFalse(os.path.exists(stray))
        recs = self.by_block()
        self.assertEqual(self.thumb(recs[1]["id"]), self.t_jpg)
        self.assertEqual(self.thumb(recs[800]["id"]), self.t_cr3)

    def test_reuse_copies_thumbs_with_new_ids(self):
        self.run_catalog()
        old = self.by_block()
        known = cat.read_jsonl(os.path.join(self.session, "catalog.jsonl"))
        new = os.path.join(self.tmp.name, "new")
        self.assertEqual(cat.catalog(self.img, new, 0, 1400, fresh=True, known=known, known_dir=self.session), 0)
        got = {r["offset"] // BS: r for r in cat.read_jsonl(os.path.join(new, "catalog.jsonl"))}
        self.assertTrue(got[1].get("reused") and got[1].get("thumb"))
        with open(cat.thumb_path(new, got[1]["id"]), "rb") as f:
            self.assertEqual(f.read(), self.t_jpg)
        self.assertFalse(got[200].get("thumb"))
        self.assertEqual(len(old), len(got))

    def test_reuse_from_same_folder_finds_archived_thumbs(self):
        self.run_catalog()
        known = cat.read_jsonl(os.path.join(self.session, "catalog.jsonl"))
        self.assertEqual(self.run_catalog(fresh=True, known=known, known_dir=self.session), 0)
        recs = self.by_block()
        self.assertTrue(recs[1].get("reused"))
        self.assertEqual(self.thumb(recs[1]["id"]), self.t_jpg)
        self.assertEqual(self.thumb(recs[800]["id"]), self.t_cr3)

    def test_tiff_thumbnail_rejects_bad_pointers(self):
        from apfsrecover.exif import tiff_thumbnail
        from apfsrecover.formats import clean_thumb
        t = synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35", thumb=self.t_jpg)
        self.assertEqual(clean_thumb(tiff_thumbnail(t)), self.t_jpg)
        self.assertIsNone(tiff_thumbnail(t[:len(t) - 10]))           # 미리보기가 잘림 → 범위 밖
        self.assertIsNone(tiff_thumbnail(b"II*\0\xff\xff\xff\xff"))   # IFD 위치가 엉터리
        self.assertIsNone(tiff_thumbnail(b""))
        self.assertIsNone(clean_thumb(b"\xff\xd8\xff" + b"\0" * 200))  # JPEG 처럼 시작하지만 깨짐


class OrientTest(unittest.TestCase):
    def test_orientation_is_read_only_when_not_normal(self):
        for v, want in ((6, 6), (8, 8), (3, 3), (1, None), (9, None)):
            t = synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35", orient=v)
            self.assertEqual(parse_tiff(t).get("orient"), want, v)
            self.assertEqual(parse_tiff(t)["taken"], "2024-03-06T09:38:35")  # 다른 값은 그대로 읽힌다
