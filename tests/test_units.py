"""장치 목록: 장치마다 기록 폴더를 따로 두고, 연결 안 된 장치의 기록도 보여 주며, 옛 기록에서 꺼낸 파일은 다시 꺼내지 않는다."""
import os
import random
import tempfile
import unittest
from datetime import datetime

from apfsrecover import extract as ex
from apfsrecover.server import App, Job
from . import synth


class UnitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = random.Random(8)
        self.a = os.path.join(self.tmp.name, "a.img")
        self.b = os.path.join(self.tmp.name, "b.img")
        synth.build_image(self.a, [(2, synth.make_jpeg(r, 300_000)), (200, synth.make_mp4(r, 400_000, datetime(2024, 1, 1)))], 600)
        synth.build_image(self.b, [(5, synth.make_jpeg(r, 250_000))], 400)
        self.root = os.path.join(self.tmp.name, "recovered")

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, **images):
        return App(os.path.join(self.root, "session"), images=images, sample_step=64 * 1024)

    def select(self, app, name):
        key = next(u["key"] for u in app.units() if u["device_id"] == "image:" + name)
        app.session = os.path.realpath(app.unit_session(key))
        return app.find_device("image:" + name)

    def test_each_device_gets_its_own_records(self):
        app = self.app(a=self.a, b=self.b)
        self.assertEqual([u["sid"] for u in app.units()], [None, None])              # 아직 검사 전
        dev = self.select(app, "a")
        self.assertEqual(app.view()._scan(Job("scan"), dev, False, False)[0], "done")
        dev = self.select(app, "b")
        self.assertEqual(app.view()._scan(Job("scan"), dev, False, False)[0], "done")
        us = {u["device_id"]: u for u in app.units()}
        self.assertNotEqual(us["image:a"]["sid"], us["image:b"]["sid"])              # 폴더가 따로
        self.assertEqual((us["image:a"]["found"], us["image:b"]["found"]), (2, 1))
        # a 를 빼면 기록만 있는 장치로 남는다
        app2 = self.app(b=self.b)
        rec = [u for u in app2.units() if not u["connected"]]
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["sid"], rec[0]["found"]), (us["image:a"]["sid"], 2))
        self.assertTrue(os.path.isdir(app2.unit_session(rec[0]["key"])))

    def test_job_keeps_its_own_records_when_view_changes(self):
        app = self.app(a=self.a, b=self.b)
        dev = self.select(app, "a")
        v = app.view()                                   # 작업은 시작할 때의 기록 폴더에 묶인다
        self.select(app, "b")                            # 화면에서 다른 장치를 골라도
        self.assertEqual(v._scan(Job("scan"), dev, False, False)[0], "done")
        self.assertTrue(os.path.isfile(os.path.join(v.session, "catalog.jsonl")))
        self.assertFalse(os.path.isfile(os.path.join(app.session, "catalog.jsonl")))

    def test_linked_old_records_are_not_extracted_again(self):
        app = self.app(a=self.a)
        dev = self.select(app, "a")
        old = app.session
        app.view()._scan(Job("scan"), dev, False, False)
        dest = os.path.join(self.tmp.name, "out")
        plan = {"dest": dest, "kinds": ["jpeg"], "include_suspect": True, "batch": {"unit": "n", "size": 10 ** 9}}
        self.assertEqual(ex.extract_batch(dev["path"], old, plan)["files"], 1)
        new = os.path.join(self.root, "session_new")
        nv = app.view(new)
        self.assertEqual(nv._scan(Job("scan"), dev, False, False, reuse=old)[0], "done")   # 이전 결과 활용
        self.assertEqual(len(ex.linked_done(new)), 1)                                    # 옛 기록에서 꺼낸 사진
        chk = ex.check_plan(new, dict(plan, kinds=["jpeg", "video-mp4"]))
        self.assertEqual(chk["overview"]["pending_files"], 1)                            # 영상만 남았다
        self.assertEqual(nv.result_summary()["files"], 1)                                # 결과는 옛 기록까지 합쳐 보여 준다


if __name__ == "__main__":
    unittest.main()
