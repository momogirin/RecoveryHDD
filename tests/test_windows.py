"""윈도우 지원(P6) 테스트. 실제 디스크 없이 가짜 GPT 이미지와 가짜 PowerShell 출력으로 검사한다."""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib

from apfsrecover import devices, fsbrowse, gpt, lock, power, privilege
from apfsrecover.blockdev import ReadOnlyDevice, device_gone, split_device_path

APFS = "7C3457EF-0000-11AA-AA11-00306543ECAC"
EFI = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"


def make_gpt_image(path, ss=512, parts=((EFI, 2048, 411647), (APFS, 411648, 4000000)), total_lba=4100000,
                   corrupt_header=False, corrupt_entries=False, n=8, esize=128):
    """실제 크기 대신 앞부분만 쓴 GPT 디스크 이미지(나머지는 희소 파일)."""
    table = bytearray(n * esize)
    for i, (tg, first, last) in enumerate(parts):
        e = uuid.UUID(tg).bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
        e += ("part%d" % i).encode("utf-16-le")
        table[i * esize:i * esize + len(e)] = e
    ecrc = zlib.crc32(bytes(table)) & 0xFFFFFFFF
    if corrupt_entries:
        ecrc ^= 1
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    struct.pack_into("<IIII", hdr, 8, 0x10000, 92, 0, 0)
    struct.pack_into("<QQQQ", hdr, 24, 1, total_lba - 1, 34, total_lba - 34)
    hdr[56:72] = uuid.uuid4().bytes_le
    struct.pack_into("<QIII", hdr, 72, 2, n, esize, ecrc)
    crc = zlib.crc32(bytes(hdr)) & 0xFFFFFFFF
    if corrupt_header:
        crc ^= 1
    struct.pack_into("<I", hdr, 16, crc)
    with open(path, "wb") as f:
        f.write(b"\0" * ss)  # 보호 MBR 자리
        f.write(bytes(hdr).ljust(ss, b"\0"))
        f.write(bytes(table))


class BlockDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.tmp.name, "disk.img")
        self.data = bytes(range(256)) * 64  # 16KB, 값이 위치를 드러낸다
        with open(self.img, "wb") as f:
            f.write(self.data)

    def test_split_device_path(self):
        self.assertEqual(split_device_path(r"\\.\PhysicalDrive2@210763776"), (r"\\.\PhysicalDrive2", 210763776))
        self.assertEqual(split_device_path(r"\\.\PhysicalDrive2"), (r"\\.\PhysicalDrive2", 0))
        self.assertEqual(split_device_path("/dev/rdisk4s2"), ("/dev/rdisk4s2", 0))
        # 일반 경로의 @ 는 오프셋이 아니다
        self.assertEqual(split_device_path("C:\\a@123\\x.img"), ("C:\\a@123\\x.img", 0))

    def test_base_offset_makes_partition_start_zero(self):
        with ReadOnlyDevice(self.img, base=1000) as d:
            self.assertEqual(d.read_at(0, 600), self.data[1000:1600])
            self.assertEqual(d.read_at(5, 3), self.data[1005:1008])

    def test_unaligned_reads_with_4096_alignment(self):
        with ReadOnlyDevice(self.img, base=512, align=4096) as d:
            for off, n in ((0, 1), (3, 5000), (4095, 2), (4096, 4096), (10000, 100000)):
                self.assertEqual(d.read_at(off, n), self.data[512 + off:512 + off + n], (off, n))

    def test_device_gone_detection(self):
        import errno
        self.assertTrue(device_gone(OSError(errno.ENXIO, "x")))
        self.assertTrue(device_gone(OSError(errno.ENODEV, "x")))
        w = OSError(5, "x")
        w.winerror = 1167  # ERROR_DEVICE_NOT_CONNECTED
        self.assertTrue(device_gone(w))
        self.assertFalse(device_gone(OSError(errno.EIO, "불량 섹터")))  # 불량 섹터는 '끊김'이 아니다

    def test_read_past_end_is_short(self):
        with ReadOnlyDevice(self.img) as d:
            self.assertEqual(len(d.read_at(16000, 5000)), 384)
            self.assertEqual(d.read_at(20000, 10), b"")


class GptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.tmp.name, "gpt.img")

    def parse(self, **kw):
        make_gpt_image(self.img, **kw)
        with ReadOnlyDevice(self.img) as d:
            return gpt.parse_gpt(d.read_at)

    def test_512_sector_disk(self):
        r = self.parse()
        self.assertEqual(r["sector_size"], 512)
        efi, apfs = r["partitions"]
        self.assertEqual((efi["content"], efi["offset"]), ("EFI", 2048 * 512))
        self.assertEqual((apfs["content"], apfs["offset"], apfs["size"]),
                         ("Apple_APFS", 411648 * 512, (4000000 - 411648 + 1) * 512))
        self.assertEqual(apfs["number"], 2)
        self.assertEqual(apfs["name"], "part1")

    def test_4096_sector_disk(self):
        r = self.parse(ss=4096, parts=((APFS, 256, 999999),))
        self.assertEqual(r["sector_size"], 4096)
        self.assertEqual(r["partitions"][0]["offset"], 256 * 4096)

    def test_bad_crc_is_not_guessed(self):
        self.assertIsNone(self.parse(corrupt_header=True))
        self.assertIsNone(self.parse(corrupt_entries=True))

    def test_not_gpt(self):
        with open(self.img, "wb") as f:
            f.write(b"\0" * 20000)
        with ReadOnlyDevice(self.img) as d:
            self.assertIsNone(gpt.parse_gpt(d.read_at))


def fake_runner(disks):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(disks).encode("utf-8"), stderr=b"")
    return run


SYSTEM = {"Number": 0, "Size": 512110190592, "Name": "SYS", "Bus": "NVMe", "Style": "GPT", "System": True,
          "Boot": True, "Partitions": [{"Number": 3, "Offset": 122683392, "Size": 498194186240,
                                        "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}", "Letter": "C"}]}
INTERNAL = {"Number": 1, "Size": 1000204886016, "Name": "Samsung SSD", "Bus": "NVMe", "Style": "GPT",
            "System": False, "Boot": False,
            "Partitions": [{"Number": 2, "Offset": 16777216, "Size": 1000186314752,
                            "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}", "Letter": "E"}]}
EXTERNAL = {"Number": 2, "Size": 5000981078016, "Name": "Seagate Expansion", "Bus": "USB", "Style": "GPT",
            "System": False, "Boot": False,
            "Partitions": [{"Number": 1, "Offset": 20480, "Size": 209715200,
                            "GptType": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}", "Letter": "\u0000"},
                           {"Number": 2, "Offset": 210763776, "Size": 4999610314752,
                            "GptType": "{7c3457ef-0000-11aa-aa11-00306543ecac}", "Letter": "\u0000"}]}


class WindowsDevicesTest(unittest.TestCase):
    def list(self, disks, **kw):
        return devices.list_devices(runner=fake_runner(disks), platform="win32", **kw)

    def test_only_external_apfs_partition_is_listed(self):
        devs = self.list([SYSTEM, INTERNAL, EXTERNAL])
        self.assertEqual([d["id"] for d in devs], ["disk2p2"])  # 시스템·내장 SSD·EFI 는 숨김
        d = devs[0]
        self.assertEqual(d["path"], r"\\.\PhysicalDrive2@210763776")
        self.assertEqual((d["content"], d["size"], d["whole_disk"]), ("Apple_APFS", 4999610314752, "disk2"))
        self.assertEqual(d["bus"], "USB")
        self.assertIn(d["readable"], (True, False))

    def test_all_disks_shows_internal_but_never_system(self):
        ids = [d["id"] for d in self.list([SYSTEM, INTERNAL, EXTERNAL], all_disks=True)]
        self.assertEqual(ids, ["disk1p2", "disk2p2"])

    def test_other_drive_letters_of_same_disk_are_forbidden_destinations(self):
        disk = json.loads(json.dumps(EXTERNAL))
        disk["Partitions"].append({"Number": 3, "Offset": 5000000000000, "Size": 2 << 30,
                                   "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}", "Letter": "F"})
        devs = self.list([disk])
        apfs = [d for d in devs if d["id"] == "disk2p2"][0]
        self.assertEqual(apfs["mounts"], ["F:\\"])

    def test_disk_without_partition_table_is_whole_disk(self):
        raw = {"Number": 3, "Size": 2 << 40, "Name": "RAW disk", "Bus": "USB", "Style": "RAW",
               "System": False, "Boot": False, "Partitions": []}
        d = self.list([raw])[0]
        self.assertEqual((d["id"], d["path"], d["content"]), ("disk3", r"\\.\PhysicalDrive3", "RAW"))

    def test_gpt_fallback_when_os_reports_no_partitions(self):
        disk = dict(EXTERNAL, Partitions=[])
        calls = []

        def fallback(path, size):
            calls.append(path)
            return [{"number": 2, "offset": 210763776, "size": 4999610314752, "content": "Apple_APFS"}]
        devs = devices._win_devices(fake_runner([disk]), False, gpt_fallback=fallback)
        self.assertEqual(calls, [r"\\.\PhysicalDrive2"])
        self.assertEqual(devs[0]["path"], r"\\.\PhysicalDrive2@210763776")

    def test_single_disk_json_object_and_failures(self):
        self.assertEqual(len(self.list(EXTERNAL)), 1)  # PowerShell 이 배열 대신 객체 하나를 줄 때

        def failing(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"x")
        self.assertEqual(devices.list_devices(runner=failing, platform="win32"), [])

        def garbage(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=b"not json", stderr=b"")
        self.assertEqual(devices.list_devices(runner=garbage, platform="win32"), [])

    def test_small_partitions_are_skipped(self):
        disk = dict(EXTERNAL, Partitions=[dict(EXTERNAL["Partitions"][1], Size=100 << 20)])
        self.assertEqual(self.list([disk]), [])


@unittest.skipUnless(os.name == "nt", "윈도우 전용")
class WindowsOnlyTest(unittest.TestCase):
    def test_alive_check_never_kills_the_process(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(lock._alive(p.pid))
            self.assertTrue(lock._alive(p.pid))
            self.assertIsNone(p.poll(), "확인만 했는데 프로세스가 종료됨")
        finally:
            p.kill()
            p.wait()
        self.assertFalse(lock._alive(p.pid))
        self.assertTrue(lock._alive(os.getpid()))

    def test_power_status_is_known_or_unknown(self):
        self.assertIn(power.on_battery(), (True, False, None))

    def test_privilege(self):
        self.assertIn(privilege.is_admin(), (True, False))
        self.assertTrue(privilege.keep_awake())

    def test_make_dir_rejects_windows_names(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("a:b", "a?b", "con", "NUL", "com1.txt", "name.", "x|y", 'q"q', "a<b", "a>b", "a*b"):
                with self.assertRaises(ValueError, msg=bad):
                    fsbrowse.make_dir(d, bad)
            self.assertTrue(os.path.isdir(fsbrowse.make_dir(d, "사진 복구")))

    def test_places_list_other_drives_as_external(self):
        names = [p["name"] for p in fsbrowse.places() if p["external"]]
        sysdrive = os.environ.get("SystemDrive", "C:").upper()
        self.assertTrue(all(sysdrive not in n for n in names))


if __name__ == "__main__":
    unittest.main()
