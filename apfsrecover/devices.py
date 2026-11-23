"""연결된 외장 디스크 목록. 서버는 이 목록에 있는 장치만 읽는다(임의 경로 거부).

macOS: `diskutil list -plist external physical`.
Windows: PowerShell `Get-Disk`/`Get-Partition`(읽기 전용 조회). 디스크 전체(`PhysicalDriveN`)를 읽되
파티션 시작 위치를 `@오프셋` 으로 붙여서 그 파티션만 본다(blockdev.py 참고). 시스템/부팅 디스크와
내장 버스(NVMe/SATA 등)는 기본으로 숨긴다(--all-disks 로 표시).
테스트·시연용 디스크 이미지 파일은 서버 시작 옵션(--image)으로만 등록할 수 있다.
"""
import json
import os
import plistlib
import subprocess
import sys
import time

from . import gpt
from .blockdev import RAW_PREFIX, ReadOnlyDevice
from .privilege import is_admin

INTERNAL_BUS = {"nvme", "sata", "ata", "atapi", "raid", "spaces", "storage spaces", "file backed virtual",
                "virtual", "sas", "fibre channel", "iscsi"}
SKIP_CONTENT = {"EFI", "Microsoft_Reserved", "Apple_Boot"}


def _mac_devices(runner):
    out = runner(["diskutil", "list", "-plist", "external", "physical"], capture_output=True, timeout=30)
    if out.returncode != 0:
        return []
    data = plistlib.loads(out.stdout)
    devs = []
    for disk in data.get("AllDisksAndPartitions", []):
        for part in disk.get("Partitions", []):
            content = part.get("Content", "")
            size = part.get("Size", 0)
            if content in ("EFI", "Apple_Boot") or size < 1 << 30:
                continue
            ident = part["DeviceIdentifier"]
            mounts = [part["MountPoint"]] if part.get("MountPoint") else []
            devs.append({"id": ident, "path": "/dev/r" + ident, "size": size, "content": content,
                         "whole_disk": disk.get("DeviceIdentifier"), "mounts": mounts,
                         "label": part.get("VolumeName") or ""})
    return devs


WIN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @()
Get-Disk | ForEach-Object {
  $d = $_
  $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ Number = $_.PartitionNumber; Offset = [int64]$_.Offset; Size = [int64]$_.Size;
                       GptType = "$($_.GptType)"; Letter = "$($_.DriveLetter)" } })
  $out += [pscustomobject]@{ Number = $d.Number; Size = [int64]$d.Size; Name = "$($d.FriendlyName)";
                             Bus = "$($d.BusType)"; Style = "$($d.PartitionStyle)";
                             System = [bool]$d.IsSystem; Boot = [bool]$d.IsBoot; Partitions = $parts }
}
ConvertTo-Json -InputObject @($out) -Depth 4 -Compress
"""


def _win_gpt_fallback(path, size):
    """OS 가 파티션을 알려 주지 않을 때 디스크의 GPT 를 직접 읽는다(관리자 권한 필요)."""
    try:
        with ReadOnlyDevice(path) as d:
            r = gpt.parse_gpt(d.read_at)
    except OSError:
        return []
    return r["partitions"] if r else []


def _win_devices(runner, all_disks=False, gpt_fallback=_win_gpt_fallback):
    out = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_SCRIPT],
                 capture_output=True, timeout=60)
    if out.returncode != 0:
        return []
    text = out.stdout.decode("utf-8", "replace") if isinstance(out.stdout, bytes) else out.stdout
    data = json.loads(text.strip().lstrip("﻿") or "[]")
    if isinstance(data, dict):
        data = [data]
    devs = []
    for disk in data:
        if disk.get("System") or disk.get("Boot"):
            continue
        if not all_disks and str(disk.get("Bus", "")).lower() in INTERNAL_BUS:
            continue
        n = disk["Number"]
        raw = "%sPhysicalDrive%d" % (RAW_PREFIX, n)
        parts = disk.get("Partitions") or []
        if isinstance(parts, dict):
            parts = [parts]
        norm = [{"number": p["Number"], "offset": p["Offset"], "size": p["Size"],
                 "content": gpt.KNOWN.get(str(p.get("GptType", "")).strip("{}").upper(), "Unknown"),
                 "letter": (p.get("Letter") or "").strip("\x00 ")} for p in parts]
        if not norm and disk.get("Style") == "GPT":
            norm = [dict(p, letter="") for p in gpt_fallback(raw, disk.get("Size", 0))]
        mounts = [p["letter"] + ":\\" for p in norm if p["letter"]]  # 같은 디스크의 다른 드라이브에도 저장 금지
        label = disk.get("Name") or ""
        shown = [p for p in norm if p["content"] not in SKIP_CONTENT and p["size"] >= 1 << 30]
        for p in shown:
            devs.append({"id": "disk%dp%d" % (n, p["number"]), "path": "%s@%d" % (raw, p["offset"]),
                         "size": p["size"], "content": p["content"], "whole_disk": "disk%d" % n,
                         "mounts": mounts, "label": label, "bus": disk.get("Bus", "")})
        if not norm and disk.get("Size", 0) >= 1 << 30:  # 파티션 표가 없는 디스크(RAW): 전체를 대상으로
            devs.append({"id": "disk%d" % n, "path": raw, "size": disk["Size"], "content": "RAW",
                         "whole_disk": "disk%d" % n, "mounts": [], "label": label, "bus": disk.get("Bus", "")})
    return devs


WIN_CACHE_SECONDS = 3.0  # PowerShell 조회가 1~2초 걸리므로, 화면이 자주 물어도 이 간격으로만 다시 조회한다
_win_cache = {}


def _win_cached(runner, all_disks):
    if runner is not subprocess.run:  # 테스트용 가짜 실행기는 캐시하지 않는다
        return _win_devices(runner, all_disks)
    hit = _win_cache.get(all_disks)
    now = time.monotonic()
    if not hit or now - hit[0] >= WIN_CACHE_SECONDS:
        hit = (now, _win_devices(runner, all_disks))
        _win_cache[all_disks] = hit
    return [dict(d) for d in hit[1]]  # 호출한 쪽이 항목을 고쳐도 캐시가 바뀌지 않게 복사


def list_devices(images=None, runner=subprocess.run, platform=sys.platform, all_disks=False):
    devs = []
    if platform == "darwin":
        try:
            devs = _mac_devices(runner)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, KeyError):
            devs = []
    elif platform == "win32":
        try:
            devs = _win_cached(runner, all_disks)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            devs = []
    for name, path in (images or {}).items():
        devs.append({"id": "image:" + name, "path": path, "size": os.path.getsize(path),
                     "content": "disk-image", "whole_disk": None, "mounts": [], "label": name})
    for d in devs:
        if d["path"].startswith(RAW_PREFIX):
            d["readable"] = is_admin()  # 윈도우 raw 디스크는 관리자만 연다
        else:
            d["readable"] = os.access(d["path"], os.R_OK)
    return devs
