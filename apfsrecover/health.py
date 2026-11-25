"""디스크 건강 상태(SMART 등) 조회. 운영체제에 물어보기만 하고 장치에는 아무것도 쓰지 않는다.

윈도우: Get-PhysicalDisk(건강 상태) + Get-StorageReliabilityCounter(오류 수·온도·사용 시간).
맥: diskutil info 의 SMART Status.
USB 외장 하드는 연결 장치(브리지)가 SMART 정보를 막는 경우가 많다. 그때는 '정보 없음'으로 알린다(추측하지 않는다).
"""
import json
import re
import subprocess
import sys

from .devices import NO_WINDOW

WIN_HEALTH = r"""
$ErrorActionPreference = 'SilentlyContinue'
$d = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq '%d' } | Select-Object -First 1
if (-not $d) { '{}' ; exit }
$r = $d | Get-StorageReliabilityCounter
[pscustomobject]@{ Health = "$($d.HealthStatus)"; Op = ($d.OperationalStatus -join ','); Media = "$($d.MediaType)"; Bus = "$($d.BusType)";
  Temp = $r.Temperature; TempMax = $r.TemperatureMax; ReadErr = $r.ReadErrorsTotal; ReadUnc = $r.ReadErrorsUncorrected;
  WriteErr = $r.WriteErrorsTotal; WriteUnc = $r.WriteErrorsUncorrected; Hours = $r.PowerOnHours; Wear = $r.Wear;
  StartStop = $r.StartStopCycleCount } | ConvertTo-Json -Compress
"""


def _disk_number(dev):
    m = re.match(r"disk(\d+)", dev.get("whole_disk") or dev.get("id") or "")
    return int(m.group(1)) if m else None


def _win(dev, runner):
    n = _disk_number(dev)
    if n is None:
        return None
    out = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_HEALTH % n], capture_output=True, timeout=60, **NO_WINDOW)
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", "replace") if isinstance(out.stdout, bytes) else out.stdout
    try:
        d = json.loads(text.strip().lstrip("﻿") or "{}")
    except ValueError:
        return None
    if not isinstance(d, dict) or not d:
        return None
    items, status = [], "unknown"
    h = (d.get("Health") or "").lower()
    if h == "healthy":
        status = "ok"
    elif h == "warning":
        status = "warn"
    elif h == "unhealthy":
        status = "bad"
    items.append(["윈도우 판정", {"healthy": "정상", "warning": "주의", "unhealthy": "위험"}.get(h, d.get("Health") or "알 수 없음")])
    has_counter = False
    for key, label, fmt in (("Temp", "온도", "%d℃"), ("Hours", "사용 시간", "%d시간"), ("ReadUnc", "고치지 못한 읽기 오류", "%d번"),
                            ("ReadErr", "읽기 오류(전체)", "%d번"), ("WriteUnc", "고치지 못한 쓰기 오류", "%d번"), ("StartStop", "켜고 끈 횟수", "%d번"),
                            ("Wear", "닳은 정도", "%d%%")):
        v = d.get(key)
        if isinstance(v, (int, float)):
            has_counter = True
            items.append([label, fmt % v])
    if isinstance(d.get("ReadUnc"), (int, float)) and d["ReadUnc"] > 0 and status == "ok":
        status = "warn"
    note = None if has_counter else "이 하드는 연결 장치(USB 등)가 자세한 SMART 정보를 막고 있어 윈도우 판정만 볼 수 있어요."
    return {"available": True, "status": status, "items": items, "note": note, "source": "Windows 저장소 관리"}


def _mac(dev, runner):
    whole = dev.get("whole_disk")
    if not whole:
        return None
    out = runner(["diskutil", "info", whole], capture_output=True, timeout=30)
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", "replace") if isinstance(out.stdout, bytes) else out.stdout
    m = re.search(r"SMART Status:\s*(.+)", text)
    v = m.group(1).strip() if m else ""
    status = {"verified": "ok", "failing": "bad"}.get(v.lower(), "unknown")
    label = {"verified": "정상", "failing": "위험", "not supported": "정보 없음"}.get(v.lower(), v or "정보 없음")
    note = "이 하드는 연결 장치(USB 등)가 SMART 정보를 막고 있어요." if v.lower() in ("", "not supported") else None
    return {"available": status != "unknown", "status": status, "items": [["SMART 상태", label]], "note": note, "source": "diskutil"}


def health(dev, runner=subprocess.run, platform=sys.platform):
    """반환: {"available", "status": ok|warn|bad|unknown, "items": [[이름, 값]], "note", "source"}"""
    if not dev or dev.get("content") == "disk-image":
        return {"available": False, "status": "unknown", "items": [], "note": "디스크 이미지 파일이라 건강 정보가 없어요.", "source": None}
    try:
        r = _win(dev, runner) if platform == "win32" else _mac(dev, runner) if platform == "darwin" else None
    except (OSError, subprocess.SubprocessError):
        r = None
    return r or {"available": False, "status": "unknown", "items": [], "note": "건강 정보를 읽지 못했어요. 관리자 권한이 아니거나 연결 장치가 정보를 막고 있을 수 있어요.", "source": None}
