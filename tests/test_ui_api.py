"""새 화면(결과 요약, 미리보기, 폴더 열기, 보고서 저장)이 쓰는 서버 기능 테스트."""
import http.client
import io
import json
import os
import random
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock
from urllib.parse import quote

from apfsrecover import fsbrowse
from apfsrecover import server as srv
from apfsrecover.server import App, serve
from . import synth

try:
    from PIL import Image
except ImportError:  # 미리보기 크기 검사만 건너뛴다
    Image = None


def real_jpeg(w=1280, h=960):
    im = Image.new("RGB", (w, h), (120, 160, 210))
    px = im.load()
    rnd = random.Random(1)
    for _ in range(40000):  # 20KB 미만 JPEG 는 썸네일로 보고 건너뛰므로 잡음을 넣어 키운다
        px[rnd.randrange(w), rnd.randrange(h)] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class UiApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        r = random.Random(4)
        files = [(2, synth.make_jpeg(r, 200_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35"))),
                 (300, synth.make_mp4(r, 700_000, datetime(2024, 3, 6, 5, 0, 0))),
                 (600, synth.make_mp4(r, 400_000, datetime(2024, 3, 6), moov=False))]  # 의심
        if Image:
            files.append((900, real_jpeg()))
        cls.img = os.path.join(cls.tmp.name, "demo.img")
        synth.build_image(cls.img, files, 1400)
        cls.session = os.path.join(cls.tmp.name, "session")
        cls.dest = os.path.join(cls.tmp.name, "out")
        cls.app = App(cls.session, images={"demo": cls.img}, sample_step=64 * 1024)
        cls.httpd, cls.url = serve(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.plan = {"dest": cls.dest, "kinds": ["jpeg", "video-mp4"], "include_suspect": True,
                    "batch": {"unit": "n", "size": 1e9}}
        cls.n_files = len(files)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-Token": self.app.token} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw, ctype = r.read(), r.getheader("Content-Type") or ""
        c.close()
        return r.status, (json.loads(raw) if "json" in ctype else raw), ctype

    def wait_job(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            j = self.call("GET", "/api/job")[1]["job"]
            if j and j["state"] != "running":
                return j
            time.sleep(0.05)
        self.fail("작업이 끝나지 않음")

    def test_flow_progress_result_thumbs_report(self):
        # 결과가 없을 때
        self.assertEqual(self.call("GET", "/api/result")[1]["files"], 0)
        self.assertEqual(self.call("GET", "/api/recent")[1]["items"], [])
        self.assertEqual(self.call("POST", "/api/open-folder", {})[0], 404)  # 저장 폴더를 아직 모른다
        self.assertEqual(self.call("POST", "/api/report/save", {})[0], 404)

        st, j, _ = self.call("POST", "/api/scan/start", {"device_id": "image:demo"})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.wait_job()["state"], "done")
        st, j, _ = self.call("POST", "/api/extract/start", {"device_id": "image:demo", "plan": self.plan})
        self.assertEqual(st, 200, j)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        # 진행 정보에 종류별 개수가 들어 있다 (화면의 "사진 3 / 3")
        bk = job["progress"]["by_kind"]
        self.assertEqual(bk["영상"], [2, 2])
        self.assertEqual(bk["사진"][0], bk["사진"][1])

        res = self.call("GET", "/api/result")[1]
        self.assertEqual(res["files"], self.n_files)
        self.assertEqual(res["by_kind"]["영상"], 2)
        self.assertEqual(res["suspect"], 1)
        self.assertEqual(res["first_day"][:4], "2024")
        self.assertTrue(os.path.samefile(res["dest"], self.dest))
        self.assertTrue(res["samples"])

        rec = self.call("GET", "/api/recent?n=2")[1]["items"]
        self.assertEqual(len(rec), 2)
        self.assertTrue(all(i["path"] for i in rec))

        # 미리보기
        sample = next(s for s in res["samples"] if s["kind"] == "jpeg")
        q = "/api/thumb?p=" + quote(sample["path"], safe="")
        st, data, ctype = self.call("GET", q)
        self.assertEqual((st, ctype), (200, "image/jpeg"))
        if Image:
            big = next(i for i in res["samples"] if i["kind"] == "jpeg" and "blk900" in i["path"])
            st, data, _ = self.call("GET", "/api/thumb?p=" + quote(big["path"], safe=""))
            self.assertEqual(st, 200)
            w, h = Image.open(io.BytesIO(data)).size
            self.assertLessEqual(max(w, h), srv.THUMB_PX)  # 원본(1280x960)이 아니라 줄인 그림

        # 저장 폴더 밖은 어떤 방법으로도 읽을 수 없다
        outside = os.path.join(self.session, "catalog.jsonl")
        for bad in ("..\..\session\catalog.jsonl", "../session/catalog.jsonl", outside, "..", "", "\\.\PhysicalDrive0"):
            st = self.call("GET", "/api/thumb?p=" + quote(bad, safe=""))[0]
            self.assertEqual(st, 404, bad)
        self.assertEqual(self.call("GET", "/api/thumb?p=" + quote(sample["path"], safe=""), token=False)[0], 401)

        # 보고서 저장: 저장 폴더 안에 쓴다
        st, r, _ = self.call("POST", "/api/report/save", {})
        self.assertEqual(st, 200)
        self.assertTrue(os.path.samefile(os.path.dirname(r["path"]), self.dest))
        self.assertIn("복구 보고서", open(r["path"], encoding="utf-8").read())

        # 폴더 열기: 저장 폴더만 연다(탐색기는 가짜로)
        with mock.patch.object(srv, "open_folder") as op:
            st, _, _ = self.call("POST", "/api/open-folder", {"path": "C:\\Windows"})  # 요청의 경로는 무시한다
            self.assertEqual(st, 200)
            op.assert_called_once()
            self.assertTrue(os.path.samefile(op.call_args[0][0], self.dest))

    def test_thumbnail_function_handles_non_images(self):
        p = os.path.join(self.tmp.name, "notimage.jpg")
        with open(p, "wb") as f:
            f.write(b"not a jpeg" * 10)
        data = srv.thumbnail(p)
        self.assertTrue(data is None or isinstance(data, bytes))  # Pillow 가 실패해도 예외로 죽지 않는다
        self.assertIsNone(srv.thumbnail(os.path.join(self.tmp.name, "missing.jpg")))

    def test_tail_jsonl_ignores_cut_lines(self):
        p = os.path.join(self.tmp.name, "t.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for i in range(5000):
                f.write(json.dumps({"id": i, "path": "사진/%d.jpg" % i}, ensure_ascii=False) + "\n")
            f.write('{"id": 5000, "pa')  # 쓰는 도중 잘린 마지막 줄
        got = srv._tail_jsonl(p, 3)
        self.assertEqual([r["id"] for r in got], [4997, 4998, 4999])

    def test_folder_listing_gives_full_paths(self):
        root = os.path.join(self.tmp.name, "browse")
        os.makedirs(os.path.join(root, "가", "나"))
        r = fsbrowse.list_dir(root)
        self.assertEqual(r["entries"], [{"name": "가", "path": os.path.join(os.path.realpath(root), "가")}])
        r2 = fsbrowse.list_dir(r["entries"][0]["path"])  # 윈도우의 \ 구분자도 서버가 만든 경로 그대로 쓴다
        self.assertEqual(r2["entries"][0]["name"], "나")
        self.assertEqual(r2["parent"], os.path.realpath(root))

    def test_ui_is_served_and_has_no_stale_text(self):
        st, body, ctype = self.call("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8")
        for stale in ("RecoveryTool.bat", "RecoveryTool.command", "APFS 복구 도구", "~/Documents"):
            self.assertNotIn(stale, html, stale)
        self.assertIn("사진 복구 도구", html)


if __name__ == "__main__":
    unittest.main()
