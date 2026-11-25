"""정리해서 복사: 촬영 시각 이름 · 날짜 폴더 · 종류 합치기 · _확인용 · 직접 넣은 파일 · 이어서 하기 · 지문 확인."""
import hashlib
import json
import os
import tempfile
import unittest

from apfsrecover import organize as om
from apfsrecover.server import App, Job


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class OrganizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.s = os.path.join(t, "rec", "session")
        self.dest = os.path.join(t, "꺼낸 파일")
        os.makedirs(self.s)
        files = [  # (id, kind, 저장 위치, 촬영 시각, 제조사, 모델, 크기)
            (1, "jpeg", "사진/2024-05-25/jpg_1.jpg", "2024-05-25T14:30:12", "Canon", "Canon EOS R8", 200_000),
            (2, "raw-cr3", "RAW/2024-05-25/cr3_2.cr3", "2024-05-25T14:30:12", "Canon", "Canon EOS R8", 300_000),
            (3, "jpeg", "사진/2024-06-01/jpg_3.jpg", "2024-06-01T09:00:00", "FUJIFILM", "X-T5", 150_000),
            (4, "jpeg", "사진/2024-06-01/jpg_4.jpg", "2024-06-01T09:00:00", "FUJIFILM", "X-T5", 150_001),   # 같은 초: 이름 뒤에 _2
            (5, "jpeg", "사진/날짜불명/jpg_5.jpg", None, None, None, 120_000),
            (6, "jpeg", "사진/2024-06-01/jpg_6.jpg", "2024-06-01T10:00:00", None, None, 5_000),            # 작은 사진
            (7, "zip", "압축/날짜불명/zip_7.zip", None, None, None, 1000),
        ]
        cat, done = [], []
        for i, kind, rel, taken, make, model, size in files:
            data = os.urandom(size)
            put(os.path.join(self.dest, *rel.split("/")), data)
            cat.append({"id": i, "kind": kind, "offset": i * 4096, "length": size, "taken": taken, "make": make, "model": model})
            done.append({"id": i, "path": rel.replace("/", "\\"), "bytes": size, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind})
        put(os.path.join(self.dest, "압축", "날짜불명", "아이폰", "IMG_20160711_213705.mov"), b"m" * 500_000)   # 직접 푼 파일
        put(os.path.join(self.dest, "_원래이름", "x.jpg"), b"link")                                           # 건너뜀
        put(os.path.join(self.dest, "복구 보고서.txt"), b"r")
        with open(os.path.join(self.s, "catalog.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in cat)
        with open(os.path.join(self.s, "extracted.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in done)
        with open(os.path.join(self.s, "plan.json"), "w", encoding="utf-8") as f:
            json.dump({"dest": self.dest, "kinds": ["jpeg"]}, f, ensure_ascii=False)
        self.app = App(self.s, images={})
        self.out = os.path.join(t, "정리")

    def tearDown(self):
        self.tmp.cleanup()

    def rel(self, rows):
        return sorted(os.path.relpath(b, self.out) for _, b, _, _ in rows)

    def test_default_plan(self):
        rows, cnt = om.plan(self.app._org_items(), self.out, {})
        self.assertEqual(self.rel(rows), sorted([
            os.path.join("2024", "2024-05", "2024-05-25", "사진", "2024-05-25_143012_EOS R8.jpg"),
            os.path.join("2024", "2024-05", "2024-05-25", "RAW", "2024-05-25_143012_EOS R8.cr3"),
            os.path.join("2024", "2024-06", "2024-06-01", "2024-06-01_090000_X-T5.jpg"),
            os.path.join("2024", "2024-06", "2024-06-01", "2024-06-01_090000_X-T5_2.jpg"),
            os.path.join("_확인용", "날짜불명", "사진", "jpg_5.jpg"),
            os.path.join("_확인용", "작은 사진", "2024-06-01_100000.jpg"),
            os.path.join("2016", "2016-07", "2016-07-11", "2016-07-11_213705.mov"),
        ]))
        self.assertEqual(cnt["extra"], 1)

    def test_other_options(self):
        rows, cnt = om.plan(self.app._org_items(), self.out, {"rename": "orig", "layout": "day", "merge": False, "review": False,
                                                                "groups": ["photo", "docs"]})
        r = self.rel(rows)
        self.assertIn(os.path.join("사진", "2024-05-25", "jpg_1.jpg"), r)
        self.assertIn(os.path.join("사진", "날짜불명", "jpg_5.jpg"), r)
        self.assertIn(os.path.join("문서·압축", "압축", "날짜불명", "zip_7.zip"), r)
        self.assertFalse(any(x.endswith(".cr3") or x.endswith(".mov") for x in r))
        self.assertFalse(any("_원래이름" in x or "보고서" in x for x in r))

    def test_run_check_resume_and_refusals(self):
        chk = self.app.organize_check(self.out, {})
        self.assertTrue(chk["ok"], chk["reasons"])
        self.assertEqual(chk["files"], 7)
        bad = self.app.organize_check(os.path.join(self.dest, "정리"), {})
        self.assertFalse(bad["ok"])
        self.assertIn("꺼낸 폴더", bad["reasons"][0])
        before = {p: os.path.getmtime(os.path.join(dp, p)) for dp, _, fs in os.walk(self.dest) for p in fs}
        n = {"i": 0}
        job = Job("organize")
        job.cancel.is_set = lambda: (n.__setitem__("i", n["i"] + 1) or n["i"] > 3)   # 중간에 멈춤
        self.app.organize_target(self.out, {})(job)
        self.assertEqual(job.state, "cancelled")
        self.assertGreater(self.app.organize_check(self.out, {})["present"], 0)
        job = Job("organize")
        self.app.organize_target(self.out, {})(job)
        self.assertEqual(job.state, "done", job.error)
        self.assertEqual((job.result["files"], job.result["checked"], job.result["mismatch"]), (7, 7, 0))
        self.assertTrue(os.path.isfile(os.path.join(self.out, om.LIST_NAME)))
        after = {p: os.path.getmtime(os.path.join(dp, p)) for dp, _, fs in os.walk(self.dest) for p in fs}
        self.assertEqual(before, after)                                            # 꺼낸 폴더는 그대로
        # 복사본이 바뀌면 확인에서 잡는다
        rows, _ = om.plan(self.app._org_items(True), self.out, {})
        with open(rows[0][1], "r+b") as f:
            f.write(b"X")
        r = om.run(rows, self.out)
        self.assertEqual(len(r["mismatch"]), 1)


if __name__ == "__main__":
    unittest.main()
