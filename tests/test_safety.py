"""읽기 전용 원칙을 코드로 검사한다 (절대 규칙 1)."""
import glob
import os
import re
import unittest

from apfsrecover.blockdev import ReadOnlyDevice

ROOT = os.path.join(os.path.dirname(__file__), "..", "apfsrecover")
FORBIDDEN = re.compile(r"O_WRONLY|O_RDWR|O_APPEND|O_CREAT|pwrite|os\.write|os\.truncate|ftruncate|\.truncate\(")


class SafetyTest(unittest.TestCase):
    def test_no_write_primitives_in_source(self):
        bad = []
        for f in glob.glob(os.path.join(ROOT, "*.py")):
            with open(f, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    if FORBIDDEN.search(code) and '"""' not in code:
                        bad.append("%s:%d: %s" % (os.path.basename(f), n, line.strip()))
        self.assertEqual(bad, [], "쓰기 관련 호출 발견")

    def test_device_class_is_read_only(self):
        public = {n for n in dir(ReadOnlyDevice) if not n.startswith("_")}
        self.assertTrue(public <= {"SECTOR", "read_at", "close"}, public)

    def test_device_open_flags(self):
        import inspect
        src = inspect.getsource(ReadOnlyDevice)
        self.assertIn("os.O_RDONLY", src)

    def test_write_attempt_fails(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x" * 4096); f.flush()
            dev = ReadOnlyDevice(f.name)
            with self.assertRaises(OSError):
                os.write(dev._fd, b"nope")  # O_RDONLY 이므로 실패해야 한다
            dev.close()


if __name__ == "__main__":
    unittest.main()
