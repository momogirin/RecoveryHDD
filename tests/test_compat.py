"""다른 버전이 만든 기록을 읽어도 멈추지 않고, 읽지 못한 것은 조용히 사라지지 않고 알려 주는지 확인한다."""
import json
import os
import tempfile
import unittest

from apfsrecover import compat, extract as ex, verify as vf
from apfsrecover.catalog import summarize
from apfsrecover.server import App


def _w(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _messy_catalog(self):
        j = json.dumps
        _w(os.path.join(self.s, "catalog.jsonl"), [
            j({"kind": "jpeg", "length": 100, "structure": "ok", "id": 1, "offset": 4096, "taken": "2024-03-06T10:00:00"}),
            j({"id": 2, "offset": 8192, "kind": "jpeg"}),                                     # length·structure 빠짐
            j({"kind": "future-kind", "length": 50, "structure": "ok", "id": 3, "offset": 12288, "새항목": 1}),  # 모르는 종류·항목
            j({"kind": "jpeg", "length": 10, "structure": "ok", "offset": 16384}),            # id 없음 -> 쓸 수 없음
            "이건 JSON 이 아닙니다",                                                          # 깨진 줄
            j([1, 2, 3]),                                                                     # 모양이 다른 줄
            '{"kind": "jpeg", "length": 1, "id": 9, "off',                                    # 잘린 마지막 줄
        ])

    def test_catalog_with_missing_unknown_and_broken_lines(self):
        self._messy_catalog()
        s = summarize(self.s)
        self.assertEqual(s["total"], 3)                       # 쓸 수 있는 것만 센다
        self.assertEqual(s["kinds"].get("future-kind"), 1)    # 모르는 종류도 버리지 않는다
        self.assertEqual(s["kinds"]["jpeg"], 2)
        self.assertEqual(s["bytes"]["jpeg"], 100)             # 빠진 length 는 0
        self.assertEqual(s["structure"]["jpeg"].get("unknown"), 1)
        self.assertEqual(s["skipped"], 4)                     # 건너뛴 줄 수를 센다
        recs = ex.load_catalog(self.s)
        self.assertEqual([r["id"] for r in recs], [1, 2, 3])
        self.assertEqual(recs[2]["새항목"], 1)                  # 모르는 항목은 그대로 보존

    def test_extracted_verify_and_summary_with_missing_fields(self):
        j = json.dumps
        _w(os.path.join(self.s, "extracted.jsonl"), [j({"id": 1, "path": "사진/a.jpg"}), j({"path": "x"}), j({"id": 2, "path": "b", "bytes": 5})])
        done = ex.load_extracted(self.s)
        self.assertEqual(sorted(done), [1, 2])
        self.assertEqual(done[1]["bytes"], 0)
        _w(os.path.join(self.s, "verify.jsonl"), [j({"id": 1, "path": "a"}), j({"status": "fail", "id": 2, "path": "b"})])
        fails = vf.load_failures(self.s)
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["reason"], "")
        with open(os.path.join(self.s, "verify_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"total": 2, "ok": 1}, f)                # 예전 버전: 항목이 적다
        vs = vf.load_summary(self.s)
        self.assertEqual((vs["fail"], vs["unchecked"], vs["levels"], vs["tools"]), (0, 0, {}, {}))

    def test_wrong_shape_files_do_not_crash(self):
        with open(os.path.join(self.s, "plan.json"), "w", encoding="utf-8") as f:
            f.write("{깨진")
        with open(os.path.join(self.s, "device.json"), "w", encoding="utf-8") as f:
            json.dump([1, 2], f)                                 # 객체가 아니라 목록
        app = App(self.s)
        self.assertIsNone(app.saved_dest())
        self.assertIsNone(app.session_device())

    def test_report_states_versions_and_skipped(self):
        self._messy_catalog()
        text = App(self.s).report()
        self.assertIn("[형식] 보고서 v%d · 기록 v1 (이전 버전 기록을 읽음)" % compat.REPORT_FORMAT, text)
        self.assertIn("읽지 못해 건너뛴 검사 기록 4줄", text)
        self.assertIn("future-kind", text)

    def test_stamp_and_newer_session(self):
        self.assertEqual(compat.session_info(self.s)["schema"], 1)   # 번호가 없으면 이전 버전
        self._messy_catalog()
        info = compat.stamp(self.s)
        self.assertEqual(info["schema"], compat.SCHEMA)
        self.assertFalse(info["legacy"])
        meta = compat.read_json(os.path.join(self.s, compat.META))
        self.assertEqual(meta["upgraded_from"], 1)                   # 이전 기록을 이어 쓴다는 표시
        with open(os.path.join(self.s, compat.META), "w", encoding="utf-8") as f:
            json.dump({"schema": 99}, f)
        info = compat.stamp(self.s)                                   # 더 새 버전 기록은 낮추지 않는다
        self.assertTrue(info["newer"])
        self.assertEqual(compat.read_json(os.path.join(self.s, compat.META))["schema"], 99)
        self.assertIn("더 새 버전 기록", App(self.s).report())


if __name__ == "__main__":
    unittest.main()
