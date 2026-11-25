"""확인 필요 파일을 사람이 '문제없음'으로 표시: 개수에서 빠지고, 되돌릴 수 있고, 목록에 없는 파일은 거절."""
import json
import os
import tempfile
import unittest

from apfsrecover.server import App


class AckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = os.path.join(self.tmp.name, "session")
        os.makedirs(self.s)
        rows = [{"status": "ok", "path": "사진/a.jpg", "id": 1},
                {"status": "fail", "path": "문서/b.pdf", "reason": "파일이 없음", "id": 2},
                {"status": "fail", "path": "RAW/c.cr3", "reason": "끝이 잘림", "id": 3}]
        with open(os.path.join(self.s, "verify.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        with open(os.path.join(self.s, "verify_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"total": 3, "ok": 1, "fail": 2, "fixed": 0, "unchecked": 0, "levels": {}, "reasons": {}, "tools": {}}, f)
        self.app = App(self.s, images={})

    def tearDown(self):
        self.tmp.cleanup()

    def test_ack_and_undo(self):
        self.assertEqual(self.app.verify_overview()["summary"]["fail"], 2)
        self.app.set_ack(["문서/b.pdf"], True)
        vo = self.app.verify_overview()
        self.assertEqual((vo["summary"]["fail"], vo["summary"]["acked"]), (1, 1))
        self.assertEqual([f["path"] for f in vo["failures"]], ["RAW/c.cr3", "문서/b.pdf"])   # 아직 안 본 것부터
        self.assertTrue(vo["failures"][1]["ack"])
        self.assertEqual(self.app._unit_stats(self.s)[2], 1)
        self.assertIn("문제없음(직접 확인", self.app.report())
        self.app.set_ack(["문서/b.pdf"], False)
        self.assertEqual(self.app.verify_overview()["summary"]["fail"], 2)

    def test_rejects_unknown_path(self):
        with self.assertRaises(ValueError):
            self.app.set_ack(["사진/a.jpg"], True)
        self.assertFalse(os.path.exists(os.path.join(self.s, "ack.json")))


if __name__ == "__main__":
    unittest.main()
