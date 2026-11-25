"""꺼내기 전 탐색(07-roadmap C): 계획의 새 필터, 화면용 목록, 원본에서 크게 보기."""
import http.client
import io
import json
import os
import random
import tempfile
import threading
import unittest
from datetime import datetime

from apfsrecover import catalog as cat
from apfsrecover import extract as ex
from apfsrecover.server import App, Job, serve
from . import synth
from .synth import BS

try:
    from PIL import Image
except ImportError:  # Pillow 가 없으면 그림을 실제로 여는 시험만 건너뛴다
    Image = None


def real_jpeg(w=1600, h=1200, seed=1):
    im = Image.new("RGB", (w, h), (120, 160, 210))
    px = im.load()
    rnd = random.Random(seed)
    for _ in range(30000):
        px[rnd.randrange(w), rnd.randrange(h)] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def tiff(model, when):
    return synth.make_tiff("Canon" if "Canon" in model else "Apple", model, when)


class SelectTest(unittest.TestCase):
    """extract.select 의 새 필터: 카메라, 의심만, 체크를 끈 항목."""

    def setUp(self):
        self.recs = [
            {"id": 1, "kind": "jpeg", "offset": 0, "length": 10, "structure": "ok", "taken": "2024-03-06T10:00:00", "model": "Canon EOS R8"},
            {"id": 2, "kind": "jpeg", "offset": 10, "length": 10, "structure": "suspect", "taken": "2024-03-06T11:00:00", "model": "Canon EOS R8"},
            {"id": 3, "kind": "jpeg", "offset": 20, "length": 10, "structure": "ok", "taken": "2024-04-01T09:00:00", "model": "iPhone 13"},
            {"id": 4, "kind": "jpeg", "offset": 30, "length": 10, "structure": "ok", "taken": None, "model": None},
            {"id": 5, "kind": "raw-cr3", "offset": 40, "length": 10, "structure": "ok", "taken": "2024-03-06T10:00:00", "model": "Canon EOS R8"},
        ]

    def ids(self, **plan):
        return [r["id"] for r in ex.select(self.recs, dict({"kinds": ["jpeg", "raw-cr3"], "include_suspect": True}, **plan))]

    def test_old_plans_behave_the_same(self):
        self.assertEqual(self.ids(), [1, 2, 3, 4, 5])
        self.assertEqual(self.ids(include_suspect=False), [1, 3, 4, 5])

    def test_models(self):
        self.assertEqual(self.ids(models=["Canon EOS R8"]), [1, 2, 5])  # 카메라를 고르면 카메라 정보 없는 파일은 빠진다
        self.assertEqual(self.ids(models=["iPhone 13", "Canon EOS R8"]), [1, 2, 3, 5])
        self.assertEqual(self.ids(models=[]), [1, 2, 3, 4, 5])  # 빈 목록 = 모두

    def test_only_suspect(self):
        self.assertEqual(self.ids(only_suspect=True), [2])
        self.assertEqual(self.ids(only_suspect=True, include_suspect=False), [2])

    def test_exclude_ids(self):
        self.assertEqual(self.ids(exclude_ids=[2, 5]), [1, 3, 4])
        self.assertEqual(self.ids(exclude_ids=["3", "x", None, 999]), [1, 2, 4, 5])  # 잘못된 값은 무시

    def test_month_range_without_undated(self):
        # 화면의 타임라인: 2024년 3월만 = date_from 03-01, date_to 03-31, 날짜 없는 파일 제외
        self.assertEqual(self.ids(date_from="2024-03-01", date_to="2024-03-31", include_undated=False), [1, 2, 5])


class BrowseBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        r = random.Random(9)
        cls.thumb = synth.make_thumb(r)
        cls.big = real_jpeg() if Image else synth.make_jpeg(r, 400_000)
        prvw = real_jpeg(1620, 1080, seed=2) if Image else None
        files = [
            (2, synth.make_jpeg(r, 300_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35", thumb=cls.thumb))),
            (200, synth.make_cr3(r, 900_000, synth.make_tiff("Canon", "Canon EOS R8", "2024:03:06 09:38:35", orient=6),
                                 tiff("Canon EOS R8", "2024:03:06 09:38:35"),
                                 thumb=cls.thumb, prvw=prvw)),
            (600, synth.make_mp4(r, 700_000, datetime(2024, 3, 6, 16, 30, 0))),
            (900, cls.big),
        ]
        cls.img = os.path.join(cls.tmp.name, "demo.img")
        synth.build_image(cls.img, files, 1600)
        cls.app = App(os.path.join(cls.tmp.name, "session"), images={"demo": cls.img}, sample_step=64 * 1024)
        cls.dev = cls.app.devices()[0]
        assert cls.app._scan(Job("scan"), cls.dev, False, False)[0] == "done"
        cls.recs = {rr["offset"] // BS: rr for rr in ex.load_catalog(cls.app.session)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class BrowseItemsTest(BrowseBase):
    def test_rows_match_catalog(self):
        out = cat.browse_items(self.app.session)
        self.assertEqual(out["fields"], ["id", "kind", "length", "taken", "model", "suspect", "thumb", "orient"])
        rows = {row[0]: dict(zip(out["fields"], row)) for row in out["rows"]}
        self.assertEqual(len(rows), len(self.recs))
        jpg = rows[self.recs[2]["id"]]
        self.assertEqual((jpg["kind"], jpg["taken"], out["models"][jpg["model"]], jpg["suspect"], jpg["thumb"]),
                         ("jpeg", "2024-03-06T09:38:35", "Canon EOS R8", 0, 1))
        vid = rows[self.recs[600]["id"]]
        local = ex.to_local("2024-03-06T16:30:00", True).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(vid["taken"], local)  # 영상의 UTC 시각은 현지 시각으로 바꿔서 보낸다(폴더·이름과 같은 날짜)
        self.assertEqual(vid["model"], -1)
        self.assertEqual(rows[self.recs[900]["id"]]["taken"], None)
        self.assertEqual(rows[self.recs[200]["id"]]["orient"], 6)   # CR3 의 CMT1 방향
        self.assertEqual(jpg["orient"], 0)                          # 방향 표시 없음 = 그대로

    def test_cache_follows_catalog_changes(self):
        a = cat.browse_items(self.app.session)
        self.assertIs(cat.browse_items(self.app.session), a)  # 바뀌지 않았으면 다시 읽지 않는다
        path = os.path.join(self.app.session, "catalog.jsonl")
        with open(path, "rb") as f:
            orig = f.read()
        self.addCleanup(lambda: open(path, "wb").write(orig))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": 999, "kind": "jpeg", "offset": 1500 * BS, "length": 4096, "structure": "ok"}) + "\n")
        b = cat.browse_items(self.app.session)
        self.assertNotEqual(a["version"], b["version"])
        self.assertEqual(len(b["rows"]), len(a["rows"]) + 1)
        self.assertEqual(cat.item_location(self.app.session, 999), (1500 * BS, 4096, "jpeg", 0))


@unittest.skipUnless(Image, "Pillow 가 없음")
class PreviewTest(BrowseBase):
    def test_jpeg_preview_is_reduced(self):
        data = self.app.preview(self.dev, self.recs[900]["id"])
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertLessEqual(max(im.size), 1200)
            self.assertEqual(im.size, (1200, 900))

    def test_cr3_uses_prvw_and_its_orientation(self):
        data = self.app.preview(self.dev, self.recs[200]["id"])
        with Image.open(io.BytesIO(data)) as im:
            # 1620x1080 PRVW 를 줄인 것(160x120 THMB 가 아니다). PRVW 에는 방향이 없어 CMT1 의 방향(6=세로)으로 돌렸다
            self.assertEqual(im.size, (800, 1200))

    def test_refusals(self):
        from apfsrecover.server import PreviewError
        cases = [(self.recs[600]["id"], 415), (424242, 404)]
        for rid, code in cases:
            with self.assertRaises(PreviewError) as c:
                self.app.preview(self.dev, rid)
            self.assertEqual(c.exception.code, code)
        self.app.job = Job("extract")  # 작업 중에는 원본을 읽지 않는다
        try:
            with self.assertRaises(PreviewError) as c:
                self.app.preview(self.dev, self.recs[900]["id"])
            self.assertEqual(c.exception.code, 409)
        finally:
            self.app.job = None

    def test_wrong_disk_is_refused(self):
        from apfsrecover.server import PreviewError
        p = os.path.join(self.app.session, "device.json")
        with open(p, encoding="utf-8") as f:
            orig = f.read()
        self.addCleanup(lambda: open(p, "w", encoding="utf-8").write(orig))
        dj = json.loads(orig)
        dj["fingerprint"] = "다른 디스크"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(dj, f)
        with self.assertRaises(PreviewError) as c:
            self.app.preview(self.dev, self.recs[900]["id"])
        self.assertEqual(c.exception.code, 400)


class BrowseHttpTest(BrowseBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.httpd, _ = serve(cls.app, 0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def call(self, path, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        c.request("GET", path, headers={"X-Token": self.app.token} if token else {})
        r = c.getresponse()
        raw, ctype = r.read(), r.getheader("Content-Type") or ""
        c.close()
        return r.status, (json.loads(raw) if "json" in ctype else raw), ctype

    def test_items_endpoint(self):
        st, out, _ = self.call("/api/catalog/items")
        self.assertEqual(st, 200)
        self.assertEqual(len(out["rows"]), len(self.recs))
        self.assertEqual(self.call("/api/catalog/items", token=False)[0], 401)

    def test_preview_endpoint(self):
        q = "/api/preview?device_id=%s&id=" % self.dev["id"]
        st, data, ctype = self.call(q + str(self.recs[900]["id"]))
        if Image:
            self.assertEqual((st, ctype), (200, "image/jpeg"))
        self.assertEqual(self.call(q + str(self.recs[600]["id"]))[0], 415)
        self.assertEqual(self.call(q + "abc")[0], 404)
        self.assertEqual(self.call("/api/preview?device_id=nope&id=1")[0], 400)
        self.assertEqual(self.call(q + str(self.recs[900]["id"]), token=False)[0], 401)


if __name__ == "__main__":
    unittest.main()
