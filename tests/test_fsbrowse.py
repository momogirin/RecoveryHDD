import os
import tempfile
import threading
import unittest

from apfsrecover import fsbrowse, guards
from apfsrecover.server import App, serve
from .test_server import ServerTest


class FsBrowseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        for d in ("alpha", "Beta", ".hidden"):
            os.mkdir(os.path.join(self.root, d))
        open(os.path.join(self.root, "file.txt"), "w").close()
        os.symlink(self.root, os.path.join(self.root, "loop"))

    def test_lists_only_visible_real_folders(self):
        r = fsbrowse.list_dir(self.root)
        self.assertEqual(r["dirs"], ["alpha", "Beta"])  # 파일·숨김·심볼릭 링크 제외, 대소문자 무시 정렬
        self.assertEqual(r["parent"], os.path.dirname(self.root))
        self.assertTrue(r["writable"])
        self.assertEqual(r["problems"], [])

    def test_problem_locations_are_browsable_but_flagged(self):
        r = fsbrowse.list_dir(guards.REPO)
        self.assertTrue(any("저장소" in x for x in r["problems"]))
        r = fsbrowse.list_dir(r"C:\Windows" if os.name == "nt" else "/System")
        self.assertTrue(any("시스템" in x for x in r["problems"]))
        r = fsbrowse.list_dir(self.root, forbidden=[self.root])
        self.assertTrue(any("원본 디스크" in x for x in r["problems"]))

    def test_missing_folder(self):
        with self.assertRaises(FileNotFoundError):
            fsbrowse.list_dir(os.path.join(self.root, "nope"))

    def test_mkdir_rules(self):
        made = fsbrowse.make_dir(self.root, "새 폴더")
        self.assertTrue(os.path.isdir(made))
        for bad in ("", "  ", "..", ".", ".hidden", "a/b", "a\\b", "x" * 101, "tab\tname", "../escape"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fsbrowse.make_dir(self.root, bad)
        with self.assertRaises(ValueError):
            fsbrowse.make_dir(self.root, "alpha")  # 이미 있음
        with self.assertRaises(ValueError):
            fsbrowse.make_dir(guards.REPO, "recovered")  # 저장소 안
        with self.assertRaises(ValueError):
            fsbrowse.make_dir("/System", "x")
        with self.assertRaises(ValueError):
            fsbrowse.make_dir(self.root, "ok", forbidden=[self.root])  # 원본 디스크 안
        self.assertFalse(os.path.exists(os.path.join(guards.REPO, "recovered")))

    def test_places_have_home_and_flags(self):
        ps = fsbrowse.places()
        self.assertEqual(ps[0]["name"], "홈")
        self.assertTrue(all("problems" in p and "free" in p for p in ps))


class FsEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls.tmp.name)
        cls.app = App(os.path.join(cls.root, "sess"), images={}, sample_step=64 * 1024)
        cls.httpd, _ = serve(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    call = ServerTest.call

    def test_endpoints(self):
        st, r = self.call("GET", "/api/fs/places")
        self.assertEqual(st, 200)
        self.assertTrue(r["places"])
        st, r = self.call("GET", "/api/fs/list?path=" + self.root)
        self.assertEqual((st, r["path"]), (200, self.root))
        st, r = self.call("GET", "/api/fs/list?path=" + self.root + "/nope")
        self.assertEqual(st, 404)
        st, r = self.call("POST", "/api/fs/mkdir", {"parent": self.root, "name": "photos"})
        self.assertEqual(st, 200)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "photos")))
        st, r = self.call("POST", "/api/fs/mkdir", {"parent": self.root, "name": "../x"})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/fs/mkdir", {"parent": guards.REPO, "name": "x"})
        self.assertEqual(st, 400)
        self.assertEqual(self.call("GET", "/api/fs/list?path=" + self.root, token=False)[0], 401)


if __name__ == "__main__":
    unittest.main()
