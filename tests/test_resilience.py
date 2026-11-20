import json
import os
import subprocess
import tempfile
import threading
import unittest

from apfsrecover import lock
from apfsrecover.power import on_battery
from apfsrecover.server import App, serve
from .test_server import ServerTest


class LockTest(unittest.TestCase):
    def test_second_instance_is_refused_and_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            # 살아 있는 다른 프로세스(부모 프로세스)가 잡고 있는 것처럼
            with open(os.path.join(d, ".lock"), "w") as f:
                f.write(str(os.getppid()))
            with self.assertRaises(RuntimeError):
                lock.acquire(d)
            # 죽은 프로세스의 잠금은 이어받는다
            p = subprocess.Popen(["true"])
            p.wait()
            with open(os.path.join(d, ".lock"), "w") as f:
                f.write(str(p.pid))
            path = lock.acquire(d)
            self.assertEqual(open(path).read(), str(os.getpid()))
            lock.release(path)
            self.assertFalse(os.path.exists(path))


class PowerTest(unittest.TestCase):
    def fake(self, text):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=text, stderr="")

    def test_parse(self):
        self.assertTrue(on_battery(self.fake("Now drawing from 'Battery Power'\n -InternalBattery-0 86%"), "darwin"))
        self.assertFalse(on_battery(self.fake("Now drawing from 'AC Power'\n"), "darwin"))
        self.assertIsNone(on_battery(self.fake(""), "darwin"))
        self.assertIsNone(on_battery(self.fake("x"), "linux"))


class InterruptedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = os.path.join(cls.tmp.name, "s")
        os.makedirs(cls.session)
        cls.app = App(cls.session, images={}, sample_step=64 * 1024)
        cls.httpd, _ = serve(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    call = ServerTest.call

    def write(self, name, obj):
        with open(os.path.join(self.session, name), "w") as f:
            json.dump(obj, f)

    def test_detects_unfinished_scan_and_extract(self):
        st, s = self.call("GET", "/api/session")
        self.assertIsNone(s["interrupted"])
        self.assertIn("on_battery", s)
        self.write("device.json", {"finished": False, "fingerprint": "x"})
        self.write("progress.json", {"state": "running", "found": 12})
        st, s = self.call("GET", "/api/session")
        self.assertEqual((s["interrupted"]["kind"], s["interrupted"]["found"]), ("scan", 12))
        self.write("progress.json", {"state": "done", "found": 12})
        self.write("device.json", {"finished": True})
        self.write("run_state.json", {"state": "running"})
        self.write("plan.json", {"dest": "/x", "kinds": ["jpeg"]})
        st, s = self.call("GET", "/api/session")
        self.assertEqual(s["interrupted"]["kind"], "extract")
        self.assertEqual(s["interrupted"]["plan"]["kinds"], ["jpeg"])
        self.write("run_state.json", {"state": "done"})
        st, s = self.call("GET", "/api/session")
        self.assertIsNone(s["interrupted"])

    def test_broken_state_file_is_reported_not_crashing(self):
        with open(os.path.join(self.session, "run_state.json"), "w") as f:
            f.write("{broken")
        st, s = self.call("GET", "/api/session")
        self.assertEqual(st, 200)
        self.assertEqual(s["interrupted"]["state"], "broken")
        os.remove(os.path.join(self.session, "run_state.json"))


if __name__ == "__main__":
    unittest.main()
