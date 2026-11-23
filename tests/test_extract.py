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
        if hasattr(time, "tzset"):  # 윈도우는 TZ 환경변수를 이 형식으로 이해하지 못해 시간대가 흔들린다
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
        days = sorted({p.split(os.sep)[1] for p in paths})
        # 영상의 촬영 시각은 UTC 03-06 16:30 이고, 폴더 날짜는 이 컴퓨터의 시간대로 바꾼 값이다(KST 면 03-07)
        video_day = ex.local_date({"taken": "2024-03-06T16:30:00", "taken_utc": True})
        # JPEG 2개(03-06, 03-07), EXIF 없는 JPEG → 날짜불명
        self.assertEqual(days, sorted({"2024-03-06", "2024-03-07", ex.UNDATED, video_day}))
        self.assertTrue(any(p.startswith(os.path.join("영상", video_day)) and p.endswith(".mp4") for p in paths))
        self.assertTrue(all(p.split(os.sep)[0] in ("사진", "영상") for p in paths))  # 종류별이 맨 위

    def test_suspect_goes_to_its_own_folder(self):
        self.run_batch(dict(self.plan, include_suspect=True, batch={"unit": "n", "size": 10}))
        paths = [r["path"] for r in ex.load_extracted(self.session).values()]
        sus = [p for p in paths if ex.SUSPECT_DIR in p.split(os.sep)]
        self.assertEqual(len(sus), 1)
        self.assertTrue(sus[0].startswith(os.path.join("영상", ex.SUSPECT_DIR)))
        self.assertFalse(any(ex.SUSPECT_DIR in p for p in paths if p not in sus))

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
        sysdir = r"C:\Windows\x" if os.name == "nt" else "/System/x"
        for bad in (os.path.join(guards.REPO, "recovered"), sysdir, os.path.abspath(os.sep), ""):
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


class RobustnessTest(ExtractBase):
    def setUp(self):
        super().setUp()
        self._delay = ex.RETRY_DELAY
        ex.RETRY_DELAY = 0  # 테스트에서는 재시도 사이에 기다리지 않는다
        self.addCleanup(setattr, ex, "RETRY_DELAY", self._delay)
        self.all_plan = dict(self.plan, kinds=["jpeg", "video-mp4"], include_suspect=True,
                             batch={"unit": "n", "size": 10})

    def _patch_read(self, fail_offsets, err):
        real = ex.ReadOnlyDevice.read_at

        def fake(self_, off, ln):
            if off in fail_offsets:
                raise OSError(err, "simulated")
            return real(self_, off, ln)
        return mock.patch.object(ex.ReadOnlyDevice, "read_at", fake)

    def test_unreadable_file_is_skipped_and_the_rest_continue(self):
        recs = ex.select(ex.load_catalog(self.session), self.all_plan)
        bad = recs[1]
        with self._patch_read({bad["offset"]}, 5):  # EIO: 불량 섹터
            r = self.run_batch(self.all_plan)
        self.assertEqual((r["state"], r["files"], r["failed"]), ("done", len(recs) - 1, 1))
        self.assertEqual(ex.load_failed(self.session), {bad["id"]})
        self.assertNotIn(bad["id"], ex.load_extracted(self.session))
        # 실패한 파일 때문에 같은 묶음이 계속 반복되지 않는다
        ov = ex.check_plan(self.session, self.all_plan)
        self.assertFalse(ov["ok"])
        self.assertEqual((ov["overview"]["failed_files"], ov["overview"]["pending_files"]), (1, 0))
        self.assertEqual([f for _, _, fs in os.walk(self.dest) for f in fs if f.endswith(".part")], [])

    def test_transient_read_error_is_retried(self):
        recs = ex.select(ex.load_catalog(self.session), self.all_plan)
        real = ex.ReadOnlyDevice.read_at
        calls = {"n": 0}

        def flaky(self_, off, ln):
            if off == recs[0]["offset"]:
                calls["n"] += 1
                if calls["n"] < 3:
                    raise OSError(5, "일시적 오류")
            return real(self_, off, ln)
        with mock.patch.object(ex.ReadOnlyDevice, "read_at", flaky):
            r = self.run_batch(self.all_plan)
        self.assertEqual((r["state"], r["failed"], r["files"]), ("done", 0, len(recs)))

    def test_many_consecutive_failures_stop_as_unstable(self):
        recs = ex.select(ex.load_catalog(self.session), self.all_plan)
        with self._patch_read({r["offset"] for r in recs}, 5):
            r = self.run_batch(self.all_plan)
        self.assertEqual(r["state"], "unstable")
        self.assertEqual(r["failed"], ex.MAX_CONSECUTIVE_FAILS)

    def test_destination_error_is_not_reported_as_source_disconnect(self):
        real_open = open

        def fake_open(path, mode="r", *a, **kw):
            if str(path).endswith(".part"):
                raise OSError(5, "저장 장치 오류")
            return real_open(path, mode, *a, **kw)
        with mock.patch("builtins.open", fake_open):
            r = self.run_batch()
        self.assertEqual(r["state"], "error")
        self.assertIn("저장 폴더", r["error"])

    def test_run_state_written(self):
        self.run_batch(self.all_plan)
        st = json.load(open(os.path.join(self.session, "run_state.json")))
        self.assertEqual(st["state"], "done")

    def test_torn_log_line_does_not_break_resume(self):
        self.run_batch()
        with open(os.path.join(self.session, "extracted.jsonl"), "a") as f:
            f.write('{"id": 77, "path": "x", "by')  # 전원이 나가서 잘린 줄
        self.assertEqual(len(ex.load_extracted(self.session)), 2)
        r = self.run_batch()
        self.assertEqual(r["state"], "done")

    def test_after_crash_last_file_with_wrong_content_is_extracted_again(self):
        self.run_batch(self.all_plan)  # 전부 꺼냄
        done = ex.load_extracted(self.session)
        victim = list(done.values())[-1]
        p = os.path.join(self.dest, victim["path"])
        good = open(p, "rb").read()
        with open(p, "r+b") as f:  # 크기는 같고 내용만 덜 써진 상황(전원 차단)
            f.seek(len(good) // 2)
            f.write(b"\0" * 64)
        json.dump({"state": "running"}, open(os.path.join(self.session, "run_state.json"), "w"))  # 비정상 종료 표시
        r = self.run_batch(self.all_plan)
        self.assertEqual((r["state"], r["files"]), ("done", 1))
        self.assertEqual(open(p, "rb").read(), good)
        self.assertEqual(ex.load_extracted(self.session)[victim["id"]]["sha256"], hashlib.sha256(good).hexdigest())

    def test_after_crash_missing_last_file_is_extracted_again_even_if_plan_looked_complete(self):
        self.run_batch(self.all_plan)
        victim = list(ex.load_extracted(self.session).values())[-1]
        os.remove(os.path.join(self.dest, victim["path"]))
        r = self.run_batch(self.all_plan)
        self.assertEqual((r["state"], r["files"]), ("done", 1))
        self.assertTrue(os.path.exists(os.path.join(self.dest, victim["path"])))

    def test_recheck_never_touches_files_outside_a_safe_destination(self):
        self.run_batch(self.all_plan)
        before = sorted(f for _, _, fs in os.walk(self.dest) for f in fs)
        r = self.run_batch(dict(self.all_plan, dest=os.path.join(ex.check_dest.__globals__["REPO"], "evil")))
        self.assertEqual(r["state"], "refused")
        self.assertEqual(sorted(f for _, _, fs in os.walk(self.dest) for f in fs), before)


class DuplicateTest(unittest.TestCase):
    """내용이 완전히 같은 파일(지웠던 옛 사본 등)은 하나만 꺼낸다."""

    def test_identical_content_is_extracted_once(self):
        r = random.Random(3)
        a = synth.make_jpeg(r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))
        b = synth.make_jpeg(r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:07 10:00:00"))
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "d.img")
            synth.build_image(img, [(1, a), (200, b), (400, a)], 700)  # 1번과 400번이 같은 내용
            sess = os.path.join(tmp, "sess")
            cat.catalog(img, sess, 0, 700, on_progress=lambda p: None)
            plan = {"dest": os.path.join(tmp, "out"), "kinds": ["jpeg"], "include_suspect": False,
                    "batch": {"unit": "n", "size": 10}}
            res = ex.extract_batch(img, sess, plan)
            self.assertEqual((res["state"], res["files"], res["duplicates"]), ("done", 2, 1))
            files = [f for _, _, fs in os.walk(plan["dest"]) for f in fs]
            self.assertEqual(len(files), 2)
            dups = ex.load_dups(sess)
            self.assertEqual(len(dups), 1)
            self.assertIn(list(dups.values())[0], [r["path"] for r in ex.load_extracted(sess).values()])
            # 다시 실행하면 중복을 또 꺼내지 않고 "모두 꺼냈습니다"가 된다
            self.assertFalse(ex.check_plan(sess, plan)["ok"])
            self.assertEqual(ex.check_plan(sess, plan)["overview"]["duplicate_files"], 1)
            res2 = ex.extract_batch(img, sess, plan)
            self.assertEqual(res2["files"], 0)
            self.assertEqual(len([f for _, _, fs in os.walk(plan["dest"]) for f in fs]), 2)
