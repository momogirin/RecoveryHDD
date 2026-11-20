import http.client
import json
import os
import random
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from apfsrecover import guards
from apfsrecover.server import App, serve
from . import synth


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        r = random.Random(11)
        files = [(2, synth.make_jpeg(r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))),
                 (120, synth.make_jpeg(r, 150_000)),
                 (300, synth.make_mp4(r, 700_000, datetime(2024, 3, 6, 5, 0, 0))),
                 (600, synth.make_cr3(r, 500_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:09 17:20:05"),
                                      synth.make_tiff("Canon", "Canon EOS R8", "2024:03:09 17:20:05")))]
        cls.img = os.path.join(cls.tmp.name, "demo.img")
        synth.build_image(cls.img, files, 1000)
        cls.session = os.path.join(cls.tmp.name, "session")
        cls.dest = os.path.join(cls.tmp.name, "out")
        cls.app = App(cls.session, images={"demo": cls.img}, sample_step=64 * 1024)
        cls.httpd, cls.url = serve(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if token:
            h["X-Token"] = self.app.token
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def wait_job(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _, j = self.call("GET", "/api/job")
            if j["job"] and j["job"]["state"] != "running":
                return j["job"]
            time.sleep(0.05)
        self.fail("작업이 끝나지 않음")

    # 순서가 의미 있는 시나리오라서 하나의 테스트로 묶는다
    def test_full_flow(self):
        # 인증
        self.assertEqual(self.call("GET", "/api/devices", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/favicon.ico", token=False)[0], 204)
        self.assertEqual(self.call("GET", "/api/devices", headers={"Host": "evil.example:%d" % self.port})[0], 403)
        self.assertEqual(self.call("GET", "/api/devices", headers={"Origin": "http://evil.example"})[0], 403)
        st, page = self.call("GET", "/?token=" + self.app.token, token=False)
        self.assertEqual(st, 200)
        # 장치
        st, d = self.call("GET", "/api/devices")
        self.assertEqual(st, 200)
        self.assertEqual([x["id"] for x in d["devices"]], ["image:demo"])
        st, e = self.call("POST", "/api/scan/start", {"device_id": "/dev/rdisk0"})  # 목록에 없는 장치
        self.assertEqual(st, 400)
        # 검사
        st, j = self.call("POST", "/api/scan/start", {"device_id": "image:demo"})
        self.assertEqual(st, 200)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["summary"]["total"], 4)
        st, s = self.call("GET", "/api/catalog/summary")
        self.assertEqual(s["kinds"], {"jpeg": 2, "video-mp4": 1, "raw-cr3": 1})
        # 계획 점검: 저장소 안·시스템 폴더는 거부
        plan = {"dest": self.dest, "kinds": ["jpeg", "raw-cr3", "video-mp4"],
                "batch": {"unit": "n", "size": 3}}
        for bad in (os.path.join(guards.REPO, "x"), "/System/x"):
            st, r = self.call("POST", "/api/plan/check", {"device_id": "image:demo", "plan": dict(plan, dest=bad)})
            self.assertEqual(st, 200)
            self.assertFalse(r["ok"], bad)
        st, r = self.call("POST", "/api/plan/check", {"device_id": "image:demo", "plan": plan})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["overview"]["files"], r["overview"]["batches_remaining"]), (4, 2))
        st, r = self.call("POST", "/api/extract/start",
                          {"device_id": "image:demo", "plan": dict(plan, dest=os.path.join(guards.REPO, "x"))})
        self.assertEqual(st, 400)
        self.assertFalse(os.path.exists(os.path.join(guards.REPO, "x")))
        # 복구: 묶음 1 → 묶음 2
        for expect in (3, 1):
            st, j = self.call("POST", "/api/extract/start", {"device_id": "image:demo", "plan": plan})
            self.assertEqual(st, 200, j)
            job = self.wait_job()
            self.assertEqual((job["state"], job["result"]["files"]), ("done", expect), job)
        st, r = self.call("POST", "/api/extract/start", {"device_id": "image:demo", "plan": plan})
        self.assertEqual(st, 400)  # 다 꺼냄
        # 검증 (합성 JPEG 라서 구조 검증)
        st, j = self.call("POST", "/api/verify/start", {"deep": False})
        self.assertEqual(st, 200, j)
        job = self.wait_job()
        self.assertEqual(job["state"], "done")
        st, v = self.call("GET", "/api/verify/summary")
        self.assertEqual((v["summary"]["total"], v["summary"]["ok"], v["summary"]["fail"]), (4, 4, 0))
        self.assertEqual(v["failures"], [])
        # 보고서
        st, rep = self.call("GET", "/api/report")
        self.assertEqual(st, 200)
        self.assertIn("열림 확인 4", rep)
        self.assertNotIn("{'", rep)  # 파이썬 딕셔너리 표기가 그대로 나오면 안 됨
        self.assertNotIn(os.path.expanduser("~") + os.sep, rep)
        # 세션 정보
        st, ses = self.call("GET", "/api/session")
        self.assertTrue(ses["has_catalog"])
        # 원본 이미지는 그대로
        self.assertTrue(os.path.getsize(self.img) == 1000 * 4096)

    def test_second_job_rejected_while_running(self):
        import apfsrecover.server as sv
        job = self.app.start_job("scan", lambda j: (time.sleep(0.4), j.finish("done")))
        st, e = self.call("POST", "/api/scan/start", {"device_id": "image:demo"})
        self.assertEqual(st, 409)
        while job.state == "running":
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()


class AutoTest(unittest.TestCase):
    """자동 복구(검사→계획 점검→복구→검증) 작업과 종료 버튼."""

    @classmethod
    def setUpClass(cls):
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        r = random.Random(21)
        files = [(2, synth.make_jpeg(r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))),
                 (120, synth.make_jpeg(r, 150_000)),
                 (300, synth.make_mp4(r, 700_000, datetime(2024, 3, 6, 5, 0, 0))),
                 (600, synth.make_cr3(r, 500_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:09 17:20:05"),
                                      synth.make_tiff("Canon", "Canon EOS R8", "2024:03:09 17:20:05")))]
        cls.img = os.path.join(cls.tmp.name, "demo.img")
        synth.build_image(cls.img, files, 1000)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.n = getattr(AutoTest, "_n", 0) + 1
        AutoTest._n = self.n
        self.session = os.path.join(self.tmp.name, "s%d" % self.n)
        self.dest = os.path.join(self.tmp.name, "out%d" % self.n)
        self.app = App(self.session, images={"demo": self.img}, sample_step=64 * 1024)
        self.httpd, _ = serve(self.app, 0)
        self.port = self.app.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.plan = {"dest": self.dest, "kinds": ["jpeg", "raw-cr3", "video-mp4"], "include_suspect": False}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    call = ServerTest.call
    wait_job = ServerTest.wait_job

    def test_auto_runs_everything(self):
        st, j = self.call("POST", "/api/auto/start", {"device_id": "image:demo", "plan": self.plan})
        self.assertEqual(st, 200, j)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        r = job["result"]
        self.assertEqual(r["files"], 4)
        v = r["verify"]
        self.assertEqual(v["total"], 4)
        self.assertEqual(v["ok"] + v["fail"], 4)  # 합성 JPEG 는 sips 가 있으면 디코딩에 실패할 수 있다(내용이 무작위)
        files = [f for _, _, fs in os.walk(self.dest) for f in fs]
        self.assertEqual(len(files), 4)
        self.assertEqual(r["catalog"]["total"], 4)

    def test_auto_stops_before_extract_when_space_is_short(self):
        real_du = guards.shutil.disk_usage
        dest_parent = os.path.realpath(self.tmp.name)  # 저장 폴더가 아직 없으면 이 폴더의 여유 공간을 본다

        def fake_du(path):
            if os.path.realpath(path) == dest_parent:  # 저장 쪽만 필요량보다 조금 모자라게
                return mock.Mock(free=5_001_000_000)
            return real_du(path)
        with mock.patch("shutil.disk_usage", fake_du):
            st, j = self.call("POST", "/api/auto/start", {"device_id": "image:demo", "plan": self.plan})
            self.assertEqual(st, 200, j)
            job = self.wait_job()
        self.assertEqual(job["state"], "needs_plan", job)
        self.assertIn("여유 공간이 부족", job["error"])
        self.assertEqual(job["result"]["overview"]["files"], 4)
        self.assertEqual(job["result"]["catalog"]["total"], 4)  # 검사 결과는 저장되어 있다
        files = [f for _, _, fs in os.walk(self.dest) for f in fs] if os.path.exists(self.dest) else []
        self.assertEqual(files, [])  # 아무것도 꺼내지 않았다

    def test_auto_rejects_bad_requests(self):
        for body in ({"device_id": "image:demo", "plan": dict(self.plan, kinds=[])},
                     {"device_id": "image:demo", "plan": dict(self.plan, dest=os.path.join(guards.REPO, "x"))},
                     {"device_id": "image:demo", "plan": dict(self.plan, dest="/System/x")},
                     {"device_id": "/dev/rdisk0", "plan": self.plan}):
            st, e = self.call("POST", "/api/auto/start", body)
            self.assertEqual(st, 400, body)
        self.assertFalse(os.path.exists(os.path.join(guards.REPO, "x")))

    def test_shutdown_refused_while_running_then_works(self):
        job = self.app.start_job("scan", lambda j: (time.sleep(0.5), j.finish("done")))
        st, e = self.call("POST", "/api/shutdown", {})
        self.assertEqual(st, 409)
        while job.state == "running":
            time.sleep(0.05)
        st, e = self.call("POST", "/api/shutdown", {})
        self.assertEqual(st, 200)
        time.sleep(0.5)
        with self.assertRaises(OSError):
            http.client.HTTPConnection("127.0.0.1", self.port, timeout=2).request("GET", "/api/devices")
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            c.request("GET", "/api/devices")
            c.getresponse()
