"""P2: 로컬 웹 서버 (127.0.0.1 전용, 토큰 필요) + 작업 관리.

디스크에 쓰는 엔드포인트는 존재하지 않는다. 장치는 목록(/api/devices)에 있는 것만 선택할 수 있고,
임의 경로를 장치로 받지 않는다. 저장 폴더는 guards.check_dest 를 통과해야 한다.
"""
import copy
import errno
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import compat
from . import devices as devmod
from . import extract as ex
from . import fsbrowse, guards, tailcheck, imaging, partfind, apfsmeta
from . import organize as orgmod
from . import health as healthmod
from . import verify as vf
from .blockdev import ReadOnlyDevice, device_gone, split_device_path
from .identity import fingerprint
from .privilege import is_admin
from .power import on_battery
from .catalog import BS, browse_items, catalog, item_location, recent_thumbs, summarize, thumb_path
from .ownership import chown_tree, expand, real_home
from .survey import estimate_extent

UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")
MARGIN_AFTER_DATA = 2 * 1000**3
MAX_BODY = 1 << 20
THUMB_PX = 240
_thumbs = {}


def _tail_jsonl(path, n, chunk=1 << 16):
    """jsonl 파일의 마지막 n 줄(큰 파일도 끝부분만 읽는다)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines()[-(n + 1):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # 잘린 첫 줄이거나 쓰는 도중인 줄
    return out[-n:]


def _inside(path, base):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:  # 윈도우: 드라이브가 다르면
        return False


def thumbnail(path):
    """저장 폴더 안의 사진 한 장의 작은 미리보기(JPEG 바이트). 영상은 첫 장면(ffmpeg 가 있을 때). 만들 수 없으면 None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _thumbs:
        return _thumbs[key]
    data = None
    if path.lower().endswith((".mp4", ".mov", ".avi")):
        from . import media
        data = media.video_frame(path, THUMB_PX)
        _thumbs[key] = data
        return data
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # JPEG 는 줄여서 읽으면 훨씬 빠르다
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_PX, THUMB_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=78)
            data = buf.getvalue()
    except Exception:  # Pillow 가 없거나 이미지가 깨짐
        if path.lower().endswith((".jpg", ".jpeg")) and st.st_size <= 8 << 20:
            with open(path, "rb") as f:
                data = f.read()  # 브라우저가 줄여서 보여 준다
    if len(_thumbs) > 400:
        _thumbs.clear()
    _thumbs[key] = data
    return data


PREVIEW_PX = 1200
PREVIEW_MAX_READ = 64 << 20   # 이보다 큰 사진은 크게 보기를 하지 않는다(디스크를 오래 붙잡지 않게)
PREVIEW_KINDS = ("jpeg", "png", "gif", "webp", "bmp", "raw-cr3")


class PreviewError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


ORIENT_ROTATE = {3: 180, 6: 270, 8: 90}  # EXIF 방향 → Pillow rotate 각도(반시계). 거울상(2·4·5·7)은 돌리지 않는다


def preview_bytes(kind, data, orient=0):
    """원본에서 읽은 파일 바이트 → 화면에 보일 JPEG 바이트. 만들 수 없으면 None.
    CR3 는 파일 안의 PRVW(1620x1080 JPEG)를 쓴다. PRVW 에는 방향 정보가 없어 검사 기록의 방향(orient)으로 돌린다.
    Pillow 가 없으면 JPEG 를 그대로 돌려준다(브라우저가 줄인다)."""
    raw = kind == "raw-cr3"
    from .formats import jpeg_parse
    if kind == "raw-cr3":
        i = data.find(b"PRVW")
        j = data.find(b"\xff\xd8\xff", i, i + 64) if i >= 0 else -1
        if j < 0:
            return None
        status, length, _ = jpeg_parse(data[j:])
        if status != "ok":
            return None
        data, kind = data[j:j + length], "jpeg"
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (PREVIEW_PX, PREVIEW_PX))
            im = ImageOps.exif_transpose(im)
            if raw and orient in ORIENT_ROTATE:
                im = im.rotate(ORIENT_ROTATE[orient], expand=True)
            im.thumbnail((PREVIEW_PX, PREVIEW_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=85)
            return buf.getvalue()
    except ImportError:
        return data if kind == "jpeg" and len(data) <= 16 << 20 else None
    except Exception:  # 깨진 그림
        return None


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - 사용자가 지정한 저장 폴더만 연다
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.sid = None  # 이 작업이 쓰는 기록 폴더 이름(장치 목록에서 어느 장치의 작업인지 알려 준다)
        self.state = "running"
        self.phase = ""
        self.error = None
        self.result = {}
        self.progress = {}
        self.cancel = threading.Event()
        self.started = time.time()
        self.finished = None
        self._phase_t0 = time.time()
        self._lock = threading.Lock()

    def set_phase(self, phase):
        with self._lock:
            self.phase = phase
            self._phase_t0 = time.time()
            self.progress = {}

    def update(self, **kw):
        with self._lock:
            self.progress.update(kw)

    def finish(self, state, error=None, **result):
        with self._lock:
            self.state, self.error, self.result = state, error, result
            self.finished = time.time()

    def to_dict(self):
        with self._lock:
            p = dict(self.progress)
            el = max(time.time() - self._phase_t0, 1e-6)
            if "done_bytes" in p and p.get("total_bytes"):
                rate = p["done_bytes"] / el
                p["rate_mb_s"] = round(rate / 1e6, 1)
                rest = p["total_bytes"] - p["done_bytes"]
                p["eta_s"] = int(rest / rate) if rate > 0 and self.state == "running" else None
            return {"id": self.id, "kind": self.kind, "session": self.sid, "state": self.state, "phase": self.phase,
                    "progress": p, "error": self.error, "result": self.result,
                    "elapsed_s": int((self.finished or time.time()) - self.started)}


class App:
    def __init__(self, session, images=None, sample_step=128 * 1024 * 1024, token=None, tail_step=32 * 1024 * 1024):
        self.session = os.path.realpath(expand(session))
        self.images = dict(images or {})
        self.sample_step = sample_step
        self.tail_step = tail_step
        self.token = token or secrets.token_urlsafe(24)
        self._shared = {"job": None, "fp": {}, "counts": {}}  # view() 로 만든 사본과 함께 쓰는 상태(작업은 한 번에 하나)
        self.lock = threading.Lock()
        self.port = None
        self.all_disks = False  # 윈도우: 내장 버스(NVMe/SATA) 디스크도 목록에 표시
        self.root = os.path.dirname(self.session)  # 장치별 기록 폴더(session*)가 모이는 곳

    @property
    def job(self):
        return self._shared["job"]

    @job.setter
    def job(self, v):
        self._shared["job"] = v

    def view(self, session=None):
        """같은 상태(작업·잠금)를 쓰되 기록 폴더가 고정된 사본. 작업은 시작할 때의 기록 폴더에만 쓴다
        (작업 중에 화면에서 다른 장치를 골라도 작업 대상이 바뀌지 않게)."""
        v = copy.copy(self)
        if session:
            v.session = os.path.realpath(session)
        return v

    # ---- 장치별 기록 ----
    def _fp_cached(self, dev, ttl=60):
        key = (dev["id"], dev["size"])
        hit = self._shared["fp"].get(key)
        if hit and time.time() - hit[1] < ttl:
            return hit[0]
        fp = self.fingerprint(dev) if dev.get("readable") else None
        if fp:
            self._shared["fp"][key] = (fp, time.time())
        return fp

    def _count_lines(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return 0
        key = (path, st.st_mtime, st.st_size)
        hit = self._shared["counts"].get(path)
        if hit and hit[0] == key:
            return hit[1]
        n = 0
        with open(path, "rb") as f:
            for _ in f:
                n += 1
        self._shared["counts"][path] = (key, n)
        return n

    def _records(self):
        """기록 폴더 목록: [{"path","sid","fp","size","label","mtime","dj"}]"""
        out = []
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return out
        for name in names:
            p = os.path.join(self.root, name)
            dj = compat.read_json(os.path.join(p, "device.json"), None) if name.startswith("session") else None
            if not dj or not dj.get("fingerprint"):
                continue
            mt = max((os.path.getmtime(os.path.join(p, f)) for f in ("device.json", "catalog.jsonl", "extracted.jsonl", "verify_summary.json", "tail_check.json")
                      if os.path.exists(os.path.join(p, f))), default=0)
            out.append({"path": p, "sid": name, "fp": dj["fingerprint"], "size": dj.get("size"), "label": dj.get("label"), "mtime": mt, "dj": dj})
        return out

    def _unit_stats(self, path):
        chain = self.view(path).related_sessions()
        found = self._count_lines(os.path.join(path, "catalog.jsonl"))
        got = sum(self._count_lines(os.path.join(s, "extracted.jsonl")) for s in chain)
        need = fixed = 0
        for s in chain:
            vs = vf.load_summary(s) or {}
            need += vs.get("fail", 0)
            fixed += vs.get("fixed", 0)
        need = max(0, need - self.view(path).acked_count())
        return found, got, need, fixed

    # ---- 확인 필요 파일을 사람이 보고 '문제없음'으로 표시 ----
    ACK_FILE = "ack.json"

    def acks(self):
        """{꺼낸 파일 경로: 표시한 시각}. 이 장치의 기록 폴더(가장 최근 것)에 둔다."""
        d = compat.read_json(os.path.join(self.session, self.ACK_FILE), {})
        return d.get("paths", {}) if isinstance(d, dict) and isinstance(d.get("paths"), dict) else {}

    def all_failures(self):
        """이어진 기록 전체의 열림 확인 실패(개수 제한 없음). 파일 수정 시각으로 기억해 둔다."""
        out = []
        for c in self.related_sessions():
            p = os.path.join(c, "verify.jsonl")
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = ("fails", p)
            hit = self._shared.get(key)
            if not hit or hit[0] != (st.st_mtime, st.st_size):
                hit = ((st.st_mtime, st.st_size), vf.load_failures(c, limit=10 ** 9))
                self._shared[key] = hit
            out += hit[1]
        return out

    def acked_count(self):
        a = self.acks()
        if not a:
            return 0
        return len({f["path"] for f in self.all_failures() if f["path"] in a})

    def set_ack(self, paths, on):
        fails = {f["path"] for f in self.all_failures()}
        bad = [p for p in paths if p not in fails]
        if bad:
            raise ValueError("확인 필요 목록에 없는 파일이에요: " + bad[0])
        a = self.acks()
        for p in paths:
            if on:
                a[p] = time.strftime("%Y-%m-%d %H:%M")
            else:
                a.pop(p, None)
        os.makedirs(self.session, exist_ok=True)
        tmp = os.path.join(self.session, self.ACK_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"paths": a}, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.session, self.ACK_FILE))
        return a

    def _devices_cached(self, fresh=False, ttl=8):
        """장치 목록(윈도우는 PowerShell 로 1~2초 걸린다)을 잠깐 기억해 둔다. 새로 고침 버튼은 fresh=True."""
        hit = self._shared.get("devs")
        if not fresh and hit and time.time() - hit[0] < ttl:
            return [dict(d) for d in hit[1]]
        devs = self.devices()
        self._shared["devs"] = (time.time(), devs)
        return [dict(d) for d in devs]

    def units(self, fresh=False):
        """장치 목록: 연결된 장치(기록이 있으면 함께) + 연결 안 됐지만 기록이 있는 장치."""
        recs = self._records()
        groups = {}
        for r in recs:
            groups.setdefault((r["fp"], r["size"]), []).append(r)
        prim = {k: max(v, key=lambda r: r["mtime"]) for k, v in groups.items()}
        job = self.job
        running_sid = job.sid if job and job.state == "running" else None
        out, used = [], set()
        for d in self._devices_cached(fresh):
            fp = self._fp_cached(d)
            k = (fp, d["size"]) if fp else None
            if not fp:  # 읽을 수 없을 때(관리자 권한 아님 등): 장치 번호와 크기가 같은 기록으로 대신 맞춘다
                k = next((kk for kk, r in prim.items() if r["dj"].get("device") == d["id"] and r["size"] == d["size"]), None)
            r = prim.get(k) if k else None
            if r:
                used.add(k)
            out.append(self._unit(d, r, running_sid))
        for k, r in sorted(prim.items(), key=lambda kv: -kv[1]["mtime"]):
            if k not in used:
                out.append(self._unit(None, r, running_sid))
        self._shared["units_last"] = out
        return out

    def _unit(self, d, r, running_sid):
        u = {"key": ("dev:" + d["id"]) if d else ("rec:" + r["sid"]), "device_id": d["id"] if d else None,
             "connected": bool(d), "readable": bool(d and d.get("readable")),
             "name": (d.get("label") if d else None) or (r and r.get("label")) or ("기록만 있는 장치" if r else "외장 디스크"),
             "size": d["size"] if d else (r or {}).get("size"), "sid": r["sid"] if r else None,
             "bus": d.get("bus") if d else None, "content": d.get("content") if d else None,
             "found": 0, "extracted": 0, "need": 0, "fixed": 0, "scanned": None, "finished": False, "last": None, "running": False}
        if r:
            found, got, need, fixed = self._unit_stats(r["path"])
            u.update(found=found, extracted=got, need=need, fixed=fixed, scanned=r["dj"].get("scanned_bytes"),
                     finished=bool(r["dj"].get("finished")), last=time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])) if r["mtime"] else None,
                     running=r["sid"] == running_sid)
        return u

    def unit_session(self, key):
        """장치 목록의 key 에 해당하는 기록 폴더 경로(없으면 새로 쓸 폴더 이름). 모르는 key 면 None.
        화면이 방금 보여 준 목록을 먼저 쓴다(장치 목록을 다시 만들면 윈도우에서 1~2초 걸려 클릭이 느려진다)."""
        last = self._shared.get("units_last") or []
        units = last if any(u["key"] == key for u in last) else self.units()
        for u in units:
            if u["key"] != key:
                continue
            if u["sid"]:
                return os.path.join(self.root, u["sid"])
            slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", u["name"] or "disk").strip("_")[:24] or "disk"
            base = "session_" + slug
            p, i = os.path.join(self.root, base), 2
            while os.path.exists(p):
                p, i = os.path.join(self.root, "%s_%d" % (base, i)), i + 1
            return p
        return None

    # ---- 장치 ----
    def devices(self):
        return devmod.list_devices(self.images, all_disks=self.all_disks)

    def find_device(self, device_id):
        for d in self._devices_cached():
            if d["id"] == device_id:
                return d
        return None

    def forbidden_mounts(self, dev):
        """원본 디스크(같은 물리 디스크의 모든 파티션)의 마운트 경로."""
        if not dev:
            return []
        out = list(dev["mounts"])
        whole = dev.get("whole_disk")
        if whole:
            for d in self._devices_cached():
                if d.get("whole_disk") == whole:
                    out += d["mounts"]
        return out

    def fingerprint(self, dev):
        try:
            return fingerprint(dev["path"], dev["size"])
        except OSError:
            return None

    def session_device(self):
        p = os.path.join(self.session, "device.json")
        return compat.read_json(p, None)

    WRONG_DISK = ("이 디스크는 검사했던 디스크와 다릅니다. 디스크를 바꿨다면 다시 검사하세요. "
                  "같은 디스크인데 이 메시지가 나오면 연결 상태를 확인하세요.")

    def disk_mismatch(self, dev):
        """검사했던 디스크와 다르면 사람이 읽을 메시지, 같거나 비교할 수 없으면 None."""
        dj = self.session_device()
        if not dj or not dj.get("fingerprint"):
            return None
        fp = self.fingerprint(dev)
        if fp is None:
            return "디스크를 읽지 못해 검사했던 디스크인지 확인할 수 없습니다. 연결과 권한을 확인하세요."
        return None if fp == dj["fingerprint"] else self.WRONG_DISK

    def preview(self, dev, rid):
        """찾은 파일 하나를 원본에서 읽어 크게 보기용 JPEG 를 만든다(읽기만 한다). 실패하면 PreviewError."""
        if self.job and self.job.state == "running":
            raise PreviewError(409, "작업 중에는 원본을 읽지 않아요(작업이 느려져요). 끝난 뒤에 다시 눌러 주세요.")
        loc = item_location(self.session, rid)
        if not loc:
            raise PreviewError(404, "검사 기록에 없는 파일입니다.")
        off, length, kind, orient = loc
        if kind not in PREVIEW_KINDS:
            raise PreviewError(415, "이 종류는 크게 볼 수 없어요. 꺼낸 뒤 열어 보세요.")
        if kind != "raw-cr3" and length > PREVIEW_MAX_READ:
            raise PreviewError(413, "파일이 너무 커서 크게 보기를 하지 않아요. 꺼낸 뒤 열어 보세요.")
        bad = self.disk_mismatch(dev)
        if bad:
            raise PreviewError(400, bad)
        try:
            with ReadOnlyDevice(dev["path"]) as d:
                data = d.read_at(off, min(length, 4 << 20) if kind == "raw-cr3" else length)
        except OSError as e:
            raise PreviewError(503, "원본을 읽지 못했어요(연결을 확인하세요): %s" % e)
        out = preview_bytes(kind, data, orient)
        if out is None:
            raise PreviewError(422, "그림을 만들 수 없어요. 파일이 깨졌을 수 있어요.")
        return out

    def interrupted(self):
        """작업이 실행 중이 아닌데 디스크에 '실행 중/연결 끊김'으로 남아 있으면 비정상 종료로 본다."""
        if self.job and self.job.state == "running":
            return None
        try:
            p = os.path.join(self.session, "progress.json")
            dj = self.session_device() or {}
            if os.path.exists(p) and dj.get("finished") is False:
                st = compat.read_json(p, None)
                if st is None:
                    return {"kind": "unknown", "state": "broken"}
                if st.get("state") in ("running", "disconnected"):
                    return {"kind": "scan", "state": st["state"], "found": st.get("found", 0)}
            p = os.path.join(self.session, "run_state.json")
            if os.path.exists(p):
                st = compat.read_json(p, None)
                if st is None:
                    return {"kind": "unknown", "state": "broken"}
                if st.get("state") in ex.UNCLEAN:
                    plan = compat.read_json(os.path.join(self.session, "plan.json"), None)
                    return {"kind": "extract", "state": st["state"], "plan": plan}
        except (OSError, ValueError):
            return {"kind": "unknown", "state": "broken"}
        return None

    # ---- 작업 ----
    def start_job(self, kind, target):
        with self.lock:
            if self.job and self.job.state == "running":
                return None
            try:
                compat.stamp(self.session)  # 기록 형식 번호를 남긴다(이전 버전 기록이면 이어 쓰기 전에 표시)
            except OSError:
                pass
            job = Job(kind)
            job.sid = os.path.basename(self.session)
            self.job = job
        session = self.session

        def run():
            try:
                target(job)
            except PermissionError:
                job.finish("error", "이 장치를 읽을 권한이 없습니다. 관리자 권한으로 다시 실행하세요(맥: RecoveryTool.command, 윈도우: RecoveryTool.bat).")
            except OSError as e:
                if device_gone(e):
                    job.finish("disconnected", "장치 연결이 끊겼습니다. 원본은 읽기만 했으므로 안전합니다. "
                               "케이블을 다시 꽂고 장치를 다시 선택한 뒤 이어서 하세요.")
                else:
                    job.finish("error", "%s" % e)
            except Exception as e:  # 작업 스레드가 조용히 죽지 않게 한다
                job.finish("error", "%s: %s" % (type(e).__name__, e))
            finally:
                chown_tree(session)
                if job.result.get("dest"):
                    chown_tree(job.result["dest"])
        threading.Thread(target=run, daemon=True).start()
        return job

    DISCONNECT_MSG = ("장치 연결이 끊겼습니다. 원본은 읽기만 했으므로 안전합니다. "
                      "케이블을 다시 꽂고 장치를 다시 선택한 뒤 이어서 하세요.")

    def _scan(self, job, dev, full, resume, reuse=None):
        """검사 한 번. 반환: (state, info) — state: done | nodata | cancelled | disconnected
        reuse: 같은 디스크의 이전 검사 세션 폴더. 그 검사에서 찾은 구간은 디스크에서 다시 읽지 않는다."""
        size = dev["size"]
        end_bytes = size
        prev = None
        fp = self.fingerprint(dev)
        djp = os.path.join(self.session, "device.json")
        if resume and os.path.exists(djp):
            prev = compat.read_json(djp, {})
            if prev.get("fingerprint") and fp and prev["fingerprint"] != fp:
                return "wrongdisk", {}
            if prev.get("size") == size and prev.get("scanned_bytes") and not prev.get("finished"):
                end_bytes = prev["scanned_bytes"]
                full = True  # 샘플링을 다시 하지 않고 이전 범위로 이어서 검사
            else:
                prev = None
        if prev is None:  # 처음부터 시작할 때: 어느 단계에서 끊겨도 같은 디스크인지 알아볼 수 있게 먼저 기록한다
            os.makedirs(self.session, exist_ok=True)
            with open(djp, "w", encoding="utf-8") as f:
                json.dump({"device": dev["id"], "label": dev.get("label"), "size": size, "fingerprint": fp, "finished": False,
                           "scanned_bytes": None, "full": bool(full)}, f)
        if not full:
            job.set_phase("sampling")
            with ReadOnlyDevice(dev["path"]) as d:
                ext = estimate_extent(
                    d, size, self.sample_step,
                    on_progress=lambda p: job.update(done_bytes=p["pos"], total_bytes=p["total"]),
                    should_cancel=job.cancel.is_set)
            if ext["cancelled"]:
                return "cancelled", {}
            if ext["last_data_offset"] is None:
                return "nodata", {}
            end_bytes = min(size, ext["last_data_offset"] + self.sample_step + MARGIN_AFTER_DATA)
        job.set_phase("cataloging")
        os.makedirs(self.session, exist_ok=True)
        kf = os.path.join(self.session, "known_from.json")
        src = compat.read_json(kf, {}).get("path") if resume else reuse
        known = ex.load_catalog(src) if src and os.path.isdir(src) else None  # 새 검사가 옛 기록을 보관(archive)하기 전에 읽어 둔다
        if not resume:
            if known and os.path.realpath(src) != os.path.realpath(self.session):
                with open(kf, "w", encoding="utf-8") as f:
                    json.dump({"path": src}, f, ensure_ascii=False)  # 끊겼다 이어할 때 같은 이전 기록을 다시 쓴다
            elif os.path.exists(kf):
                os.remove(kf)
        rc = catalog(dev["path"], self.session, 0, end_bytes // BS,
                     on_progress=lambda p: job.update(done_bytes=p["pos"], total_bytes=p["total"], found=p["found"]),
                     should_cancel=job.cancel.is_set, fresh=not resume, known=known, known_dir=src if known else None)
        with open(os.path.join(self.session, "device.json"), "w", encoding="utf-8") as f:
            json.dump({"device": dev["id"], "label": dev.get("label"), "size": size, "scanned_bytes": end_bytes, "fingerprint": fp,
                       "full": bool(full) and prev is None, "finished": rc == 0}, f)
        return {0: "done", 3: "cancelled"}.get(rc, "disconnected"), {"end_bytes": end_bytes}

    def reusable(self, dev):
        """같은 디스크를 이미 끝까지 검사한 세션 폴더(현재 세션과 같은 위치의 session* 폴더)를 최근 순으로."""
        fp = self.fingerprint(dev)
        parent = os.path.dirname(self.session)
        paths = [self.session]
        try:
            paths += [os.path.join(parent, d) for d in sorted(os.listdir(parent))
                      if d.startswith("session") and os.path.join(parent, d) != self.session]
        except OSError:
            pass
        out = []
        for p in paths:
            cat = os.path.join(p, "catalog.jsonl")
            dj = compat.read_json(os.path.join(p, "device.json"), None)
            if not dj or not os.path.isfile(cat) or not dj.get("finished") or dj.get("size") != dev["size"]:
                continue
            if not fp or dj.get("fingerprint") != fp or compat.session_info(p)["newer"]:
                continue
            found = compat.read_json(os.path.join(p, "progress.json"), {}).get("found")
            out.append({"name": "현재 세션" if p == self.session else os.path.basename(p), "path": p,
                        "found": found if isinstance(found, int) else None, "mtime": os.path.getmtime(cat)})
        return sorted(out, key=lambda x: -x["mtime"])

    def scan_target(self, dev, full, resume, reuse=None):
        def target(job):
            state, info = self._scan(job, dev, full, resume, reuse)
            if state == "done":
                job.finish("done", summary=summarize(self.session), **info)
            elif state == "nodata":
                job.finish("done", error="디스크에서 데이터를 찾지 못했습니다(샘플링 기준).", found=0, end_bytes=0)
            elif state == "disconnected":
                job.finish("disconnected", self.DISCONNECT_MSG)
            elif state == "wrongdisk":
                job.finish("refused", self.WRONG_DISK)
            else:
                job.finish(state)
        return target

    def auto_target(self, dev, plan, full, resume):
        """검사 → 계획 점검 → (전부 한 번에) 복구 → 검증을 한 작업으로 잇는다.
        저장 공간이 모자라면 복구를 시작하지 않고 needs_plan 으로 끝내 계획 화면으로 넘긴다."""
        def target(job):
            state, info = self._scan(job, dev, full, resume)
            if state == "nodata":
                return job.finish("nodata", "디스크에서 데이터를 찾지 못했습니다(샘플링 기준). '전체 검사'로 다시 시도해 보세요.")
            if state == "disconnected":
                return job.finish("disconnected", self.DISCONNECT_MSG)
            if state == "wrongdisk":
                return job.finish("refused", self.WRONG_DISK)
            if state != "done":
                return job.finish(state)
            summ = summarize(self.session)
            whole = dict(plan, batch={"unit": "gb", "size": 1e9})  # 선택한 것 전부를 한 묶음으로
            chk = ex.check_plan(self.session, whole, self.forbidden_mounts(dev))
            if not chk["ok"]:
                return job.finish("needs_plan", " / ".join(chk["reasons"]), overview=chk["overview"],
                                  free=chk["free"], need=chk["need"] + chk["margin"], catalog=summ)
            job.set_phase("extracting")
            r = ex.extract_batch(
                dev["path"], self.session, whole, forbidden=self.forbidden_mounts(dev),
                on_progress=lambda p: job.update(done_bytes=p["bytes"], total_bytes=p["total_bytes"],
                                                 files=p["files"], total_files=p["total_files"], current=p["current"]),
                should_cancel=job.cancel.is_set)
            dest = chk["dest"]
            if r["state"] != "done":
                return job.finish(r["state"], r["error"] or (self.DISCONNECT_MSG if r["state"] == "disconnected" else None),
                                  files=r["files"], bytes=r["bytes"], failed=r["failed"], duplicates=r["duplicates"], dest=dest,
                                  catalog=summ)
            job.set_phase("verifying")
            v = vf.verify_session(self.session, dest,
                                  on_progress=lambda p: job.update(done=p["done"], total=p["total"], ok=p["ok"], fail=p["fail"]),
                                  should_cancel=job.cancel.is_set, deep=True)
            job.finish("cancelled" if v["cancelled"] else "done", files=r["files"], bytes=r["bytes"],
                       failed=r["failed"], duplicates=r["duplicates"], dest=dest, verify=v, catalog=summ)
        return target

    def extract_target(self, dev, plan):
        def target(job):
            bad = self.disk_mismatch(dev)
            if bad:
                return job.finish("refused", bad)
            job.set_phase("extracting")
            r = ex.extract_batch(
                dev["path"], self.session, plan, forbidden=self.forbidden_mounts(dev),
                on_progress=lambda p: job.update(done_bytes=p["bytes"], total_bytes=p["total_bytes"],
                                                 files=p["files"], total_files=p["total_files"],
                                                 current=p["current"], by_kind=p.get("by_kind")),
                should_cancel=job.cancel.is_set)
            dest = os.path.realpath(expand(plan["dest"])) if r["state"] != "refused" else None
            job.finish(r["state"], r["error"], files=r["files"], bytes=r["bytes"], failed=r["failed"],
                       duplicates=r["duplicates"], dest=dest)
        return target

    def related_sessions(self, limit=6):
        """현재 세션 + '이전 검사 결과 재사용'으로 이어진 옛 세션들(같은 디스크). 옛 세션에서 꺼낸 파일도 같이 센다."""
        out, p = [], self.session
        while p and os.path.isdir(p) and len(out) < limit and os.path.realpath(p) not in [os.path.realpath(x) for x in out]:
            out.append(p)
            p = compat.read_json(os.path.join(p, "known_from.json"), {}).get("path")
        return out

    def format_check(self, dev=None):
        """포맷해도 되는지 점검. 항목은 늘 같은 7개, 같은 순서(화면 자리 고정).
        level: ok | warn | bad | info. 판단은 기록 파일과 저장 폴더만 읽고, 원본 디스크는 읽지 않는다."""
        items = {}
        sessions = self.related_sessions()
        cur = ex.load_catalog(self.session) if os.path.isfile(os.path.join(self.session, "catalog.jsonl")) else []
        dj = self.session_device() or {}
        if not cur:
            return {"verdict": "bad", "items": [["bad", "아직 디스크 검사 결과가 없어요."]] + [["info", "-"]] * 6, "sessions": 0}
        got, dup, failed, dests, missing, n_extracted = set(), set(), set(), [], 0, 0
        vsum = {"ok": 0, "fail": 0, "fixed": 0, "unchecked": 0}
        no_verify = 0
        sessions = [s for s in sessions if os.path.isfile(os.path.join(s, "catalog.jsonl"))]
        for s in sessions:
            recs = {r["id"]: r["offset"] for r in ex.load_catalog(s)}
            done = ex.load_extracted(s)
            got |= {recs[i] for i in done if i in recs}
            dup |= {recs[i] for i in ex.load_dups(s) if i in recs}
            failed |= {recs[i] for i in ex.load_failed(s) if i in recs}
            dest = compat.read_json(os.path.join(s, "plan.json"), {}).get("dest")
            if done and dest:
                real = os.path.realpath(expand(dest))
                if real not in dests:
                    dests.append(real)
                missing += sum(1 for r in done.values() if not os.path.isfile(os.path.join(real, r.get("path", ""))))
            n_extracted += len(done)
            vs = vf.load_summary(s)
            if done and not vs:
                no_verify += len(done)
            for k in vsum:
                vsum[k] += (vs or {}).get(k, 0)
        vsum["fail"] = max(0, vsum["fail"] - self.acked_count())
        # 1) 저장 위치가 원본 디스크와 다른가
        if not dests:
            items[1] = ["bad", "아직 꺼낸 파일이 없어요."]
        elif dev is None or self.disk_mismatch(dev):
            items[1] = ["warn", "검사했던 원본 디스크가 연결돼 있지 않아, 저장 위치가 다른 디스크인지 확인하지 못했어요."]
        else:
            forb = [f for f in self.forbidden_mounts(dev) if f]
            same = [d for d in dests if any(guards._under(d, f) for f in forb)]
            items[1] = ["bad", "꺼낸 파일이 원본 디스크 안에 있어요: " + ", ".join(same) + ". 포맷하면 같이 지워져요."] if same \
                else ["ok", "꺼낸 파일은 원본과 다른 디스크에 있어요: " + ", ".join(dests)]
        # 2) 꺼내기가 끝났는가 (각 세션의 계획 기준)
        pending = 0
        for s in sessions:
            plan = compat.read_json(os.path.join(s, "plan.json"), None)
            if plan and plan.get("kinds"):
                pending += ex.overview(ex.load_catalog(s), plan, ex.load_extracted(s), ex.load_failed(s), ex.load_dups(s))["pending_files"]
        items[2] = ["bad", "고른 종류 중 아직 안 꺼낸 파일이 %s개 있어요. 먼저 마저 꺼내세요." % format(pending, ",")] if pending \
            else ["ok", "고른 종류는 모두 꺼냈어요 (꺼낸 파일 %s개, 같은 내용이라 하나만 둔 것 %s개)." % (format(n_extracted, ","), format(len(dup), ","))]
        # 3) 꺼낸 파일이 저장 폴더에 그대로 있는가
        items[3] = ["warn", "꺼낸 파일 중 %s개가 저장 폴더에 없어요. 옮기거나 지웠는지 확인하세요." % format(missing, ",")] if missing \
            else ["ok", "꺼낸 파일이 저장 폴더에 모두 있어요."] if n_extracted else ["info", "-"]
        # 4) 열림 확인
        if no_verify:
            items[4] = ["warn", "열림 확인을 안 한 파일이 %s개 있어요. [결과 탐색]에서 [열림 다시 확인]을 누르세요." % format(no_verify, ",")]
        elif vsum["unchecked"] or vsum["fail"]:
            items[4] = ["warn", "열림 확인: 정상 %s · 복원본 %s · 확인 필요 %s · 미확인 %s. 확인 필요 파일은 포맷하면 다시 꺼낼 수 없어요." % tuple(
                format(vsum[k], ",") for k in ("ok", "fixed", "fail", "unchecked"))]
        else:
            items[4] = ["ok", "열림 확인: 모두 정상%s." % (" (ZIP 복원본 %s개 포함)" % vsum["fixed"] if vsum["fixed"] else "")] if n_extracted else ["info", "-"]
        # 5) 검사에서 찾았지만 꺼내지 않은 파일(고르지 않은 종류)
        left = {}
        for r in cur:
            if r["offset"] not in got and r["offset"] not in dup and r["offset"] not in failed:
                left[r["kind"]] = left.get(r["kind"], 0) + 1
        nleft = sum(left.values())
        items[5] = ["warn", "찾았지만 꺼내지 않은 파일 %s개 (%s). 필요 없으면 괜찮아요." % (format(nleft, ","), ", ".join(
            "%s %s" % (k, format(v, ",")) for k, v in sorted(left.items(), key=lambda x: -x[1])[:5]))] if nleft \
            else ["ok", "검사에서 찾은 파일은 모두 꺼냈어요."]
        # 6) 검사 범위
        size, end = dj.get("size"), dj.get("scanned_bytes")
        if not dj.get("finished") or not isinstance(size, int) or not isinstance(end, int):
            items[6] = ["bad", "디스크 검사가 끝나지 않았어요."]
        elif end >= size:
            items[6] = ["ok", "디스크 전체를 검사했어요."]
        else:
            items[6] = ["info", "검사한 범위: 앞쪽 %.0f GB / 전체 %.0f GB. 뒤쪽은 샘플로 보고 비어 있다고 추정했어요." % (end / 1e9, size / 1e9)]
        # 7) 끝부분 촘촘 검사
        if items[6][0] == "ok":
            items[7] = ["ok", "끝부분 검사는 필요 없어요(전체 검사함)."]
        else:
            v = tailcheck.verdict(tailcheck.load(self.session)) if tailcheck.load(self.session) else None
            items[7] = {None: ["warn", "끝부분 촘촘 검사를 아직 안 했어요. 포맷 전에 한 번 해 보길 권해요."],
                        "partial": ["warn", "끝부분 촘촘 검사를 끝까지 하지 않았어요."],
                        "empty": ["ok", "끝부분 촘촘 검사: 뒤쪽은 비어 있어요(추정)."],
                        "residue": ["warn", "끝부분 촘촘 검사: 뒤쪽에 내용이 조금 있어요. 지워진 찌꺼기일 수 있어요."],
                        "files": ["bad", "끝부분 촘촘 검사: 뒤쪽에서 파일 시작 표시를 찾았어요. 포맷 전에 그 구간을 검사하세요."]}[v]
        rows = [items[i] for i in range(1, 8)]
        levels = {r[0] for r in rows}
        return {"verdict": "bad" if "bad" in levels else "warn" if "warn" in levels else "ok", "items": rows,
                "sessions": len(sessions)}

    def tail_info(self):
        """끝부분 촘촘 검사를 할 수 있는지와 지난 결과. 검사(기본)를 끝까지 마친 세션만 대상."""
        dj = self.session_device() or {}
        size, end = dj.get("size"), dj.get("scanned_bytes")
        st = tailcheck.load(self.session)
        out = {"state": st, "verdict": tailcheck.verdict(st) if st else None, "size": size, "scanned_bytes": end,
               "step": self.tail_step, "can": False, "why": None}
        if not dj or not dj.get("finished") or not isinstance(end, int) or not isinstance(size, int):
            out["why"] = "먼저 디스크 검사를 끝까지 마쳐야 해요."
        elif end >= size:
            out["why"] = "이미 디스크 끝까지 검사했어요. 더 볼 곳이 없어요."
        else:
            out["can"] = True
            out["samples"] = (size - end) // self.tail_step
        return out

    def tail_target(self, dev, resume):
        def target(job):
            bad = self.disk_mismatch(dev)
            if bad:
                return job.finish("refused", bad)
            info = self.tail_info()
            if not info["can"]:
                return job.finish("refused", info["why"])
            job.set_phase("tailcheck")
            with ReadOnlyDevice(dev["path"]) as d:
                st = tailcheck.tail_check(
                    d, self.session, info["scanned_bytes"], info["size"], self.tail_step,
                    fingerprint=(self.session_device() or {}).get("fingerprint"), resume=resume,
                    on_progress=lambda p: job.update(done_bytes=p["pos"], total_bytes=p["total"],
                                                     nonzero=p["nonzero"], hits=p["hits"]),
                    should_cancel=job.cancel.is_set)
            job.finish("done" if st["finished"] else "cancelled", verdict=tailcheck.verdict(st),
                       samples=st["samples"], nonzero=st["nonzero"], hits=len(st["hits"]))
        return target

    def verify_target(self, dest, deep):
        def target(job):
            job.set_phase("verifying")
            s = vf.verify_session(self.session, dest,
                                  on_progress=lambda p: job.update(done=p["done"], total=p["total"],
                                                                   ok=p["ok"], fail=p["fail"]),
                                  should_cancel=job.cancel.is_set, deep=deep)
            job.finish("cancelled" if s["cancelled"] else "done", summary=s)
        return target

    # ---- 도구: 건강 상태 · 백업 · 파티션 찾기 · 원래 이름 ----
    def health_of(self, dev):
        key = ("health", dev["id"])
        hit = self._shared.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        r = healthmod.health(dev)
        self._shared[key] = (time.time(), r)
        return r

    def whole_disk_path(self, dev):
        """파티션이 아니라 디스크 전체를 읽는 경로와, 그 안에서 이 장치가 시작하는 위치."""
        base, off = split_device_path(dev["path"])
        if off:
            return base, off
        w = dev.get("whole_disk")
        if w and sys.platform == "darwin" and dev["path"].startswith("/dev/r") and w != dev["id"]:
            return "/dev/r" + w, None  # 맥: 파티션 시작 위치는 diskutil 이 알려 주지 않으면 모른다
        return dev["path"], 0

    def backup_info(self):
        plan = compat.read_json(os.path.join(self.session, "backup.json"), None)
        st = imaging.load_state(plan["image"]) if plan and plan.get("image") else None
        dj = self.session_device() or {}
        return {"plan": plan, "state": st, "scanned_bytes": dj.get("scanned_bytes") if dj.get("finished") else None}

    def backup_target(self, dev, dest_dir, whole, resume):
        def target(job):
            fp = self.fingerprint(dev)
            info = self.backup_info()
            plan = info["plan"] if resume and info["plan"] else None
            if plan is None:
                scanned = info["scanned_bytes"]
                end = dev["size"] if whole or not scanned else min(dev["size"], scanned)
                safe = re.sub(r"[^0-9A-Za-z가-힣]+", "_", dev.get("label") or dev["id"]).strip("_")[:40] or "disk"
                image = os.path.join(os.path.realpath(expand(dest_dir)), "%s_%s.img" % (safe, time.strftime("%Y%m%d_%H%M")))
                plan = {"image": image, "start": 0, "end": end, "whole": bool(whole or not scanned), "device": dev["id"]}
            st = imaging.load_state(plan["image"]) if resume else None
            have = (st["pos"] - plan["start"]) if st else 0
            chk = guards.check_dest(os.path.dirname(plan["image"]), self.forbidden_mounts(dev), plan["end"] - plan["start"] - have)
            if not chk["ok"]:
                return job.finish("refused", " / ".join(chk["reasons"]))
            os.makedirs(self.session, exist_ok=True)
            with open(os.path.join(self.session, "backup.json"), "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False)
            job.set_phase("backup")
            st = imaging.backup(dev["path"], plan["image"], plan["start"], plan["end"], fingerprint=fp, resume=resume,
                                on_progress=lambda p: job.update(done_bytes=p["pos"], total_bytes=p["total"], bad_bytes=p["bad_bytes"]),
                                should_cancel=job.cancel.is_set)
            job.finish("done" if st["finished"] else "cancelled", image=plan["image"], bad_bytes=st["bad_bytes"], bad=len(st["bad"]), dest=os.path.dirname(plan["image"]))
        return target

    def partfind_target(self, dev):
        def target(job):
            path, dev_off = self.whole_disk_path(dev)
            job.set_phase("partfind")
            with ReadOnlyDevice(path) as d:
                r = partfind.find(d.read_at, None if path != dev["path"] else dev["size"],
                                  on_progress=lambda p: job.update(done=p["done"], total=p["total"], found=p["found"]),
                                  should_cancel=job.cancel.is_set)
            r["device_offset"] = dev_off
            r["whole"] = path != dev["path"]
            os.makedirs(self.session, exist_ok=True)
            with open(os.path.join(self.session, "partitions.json"), "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False)
            job.finish("done" if r["finished"] else "cancelled", found=len(r["parts"]), table=r["table"])
        return target

    NAMES_DIR = "_원래이름"

    def names_target(self, dev):
        """APFS 파일 목록에서 원래 이름·폴더를 찾아, 꺼낸 파일과 같은 위치에서 시작하는 것에 이름을 붙인다.
        저장 폴더 안의 _원래이름 폴더에 원래 이름으로 '하드 링크'(용량을 더 쓰지 않는 같은 파일)를 만든다.
        하드 링크를 만들 수 없으면 목록 파일(목록.csv)만 남긴다. 꺼낸 파일과 원본 장치는 바꾸지 않는다."""
        def target(job):
            job.set_phase("names")
            dest = self.saved_dest()
            if not dest:
                return job.finish("refused", "먼저 파일을 꺼내야 해요.")
            dest = os.path.realpath(expand(dest))
            bases = [0]
            pj = compat.read_json(os.path.join(self.session, "partitions.json"), {})
            if pj.get("device_offset") is not None:
                for p in pj.get("parts", []):
                    if p.get("fs") == "APFS":
                        b = p["offset"] - pj["device_offset"]
                        if b >= 0 and b not in bases:
                            bases.append(b)
            res, base = None, 0
            with ReadOnlyDevice(dev["path"]) as d:
                for base in bases:
                    if job.cancel.is_set():
                        return job.finish("cancelled")
                    res = apfsmeta.read_names(d.read_at, base)
                    if res["ok"]:
                        break
            out = {"ok": bool(res and res["ok"]), "error": res and res.get("error"), "volumes": (res or {}).get("volumes", []),
                   "found": len((res or {}).get("by_block", {})), "matched": 0, "linked": 0, "total": 0, "dir": None, "base": base}
            if out["ok"]:
                bs, by_block = res["block_size"], res["by_block"]
                root = os.path.join(dest, self.NAMES_DIR)
                rows, items = [], []
                for s in self.related_sessions():
                    offs = {r["id"]: r["offset"] for r in ex.load_catalog(s)} if os.path.isfile(os.path.join(s, "catalog.jsonl")) else {}
                    for i, r in ex.load_extracted(s).items():
                        items.append((offs.get(i), r.get("path", "")))
                out["total"] = len(items)
                for n, (off, rel) in enumerate(items):
                    if job.cancel.is_set():
                        break
                    name = None
                    if off is not None and off >= base and (off - base) % bs == 0:
                        name = by_block.get((off - base) // bs)
                    result = "이름 못 찾음"
                    if name:
                        out["matched"] += 1
                        tgt = os.path.join(root, *[p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")])
                        stem, ext = os.path.splitext(tgt)
                        k = 2
                        while os.path.exists(tgt):
                            tgt, k = "%s (%d)%s" % (stem, k, ext), k + 1
                        try:
                            os.makedirs(os.path.dirname(tgt), exist_ok=True)
                            os.link(os.path.join(dest, rel), tgt)
                            out["linked"] += 1
                            result = "연결함"
                        except OSError:
                            result = "목록에만 적음(연결 실패)"
                    rows.append((rel, name or "", result))
                    if n % 200 == 0:
                        job.update(done=n, total=len(items), matched=out["matched"])
                os.makedirs(root, exist_ok=True)
                import csv
                with open(os.path.join(root, "목록.csv"), "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["꺼낸 파일", "원래 이름(볼륨/폴더/파일)", "결과"])
                    w.writerows(rows)
                out["dir"] = root
            os.makedirs(self.session, exist_ok=True)
            with open(os.path.join(self.session, "names.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            if not out["ok"]:
                return job.finish("done", error=None, **{k: out[k] for k in ("matched", "total")}, names_error=out["error"])
            job.finish("cancelled" if job.cancel.is_set() else "done", matched=out["matched"], linked=out["linked"], total=out["total"], dest=dest)
        return target

    # ---- 정리해서 복사 ----
    def _org_pairs(self):
        out = []
        for c in self.related_sessions():
            d = compat.read_json(os.path.join(c, "plan.json"), {}).get("dest")
            if d:
                out.append((c, os.path.realpath(expand(d))))
        return out

    def _org_items(self, fresh=False):
        """꺼낸 파일 목록(파일이 있는지 확인하느라 수천 개면 1~2초) 을 잠깐 기억해 둔다."""
        key = ("org", self.session)
        hit = self._shared.get(key)
        if not fresh and hit and time.time() - hit[0] < 60:
            return hit[1]
        items = orgmod.collect(self._org_pairs())
        self._shared[key] = (time.time(), items)
        return items

    def organize_check(self, out, opts, dev=None, fresh=False):
        """미리보기와 시작 전 확인. 반환: {ok, reasons, files, bytes, counts, samples, free, src, last}"""
        pairs = self._org_pairs()
        last = compat.read_json(os.path.join(self.session, "organize.json"), None)
        res = {"ok": False, "reasons": [], "files": 0, "bytes": 0, "counts": {}, "samples": [], "free": None,
               "src": [p for _, p in pairs], "last": last, "out": None}
        if not pairs:
            res["reasons"].append("먼저 파일을 꺼내야 해요.")
            return res
        items = self._org_items(fresh)
        o = orgmod.opts_of(opts)
        outp = os.path.realpath(expand(out)) if out else None
        rows, cnt = orgmod.plan(items, outp or "<저장할 곳>", o)
        res.update(files=len(rows), bytes=sum(r[2] for r in rows), counts=dict(cnt), out=outp,
                   samples=[[a, b] for a, b in orgmod.samples(rows, outp or "<저장할 곳>")])
        if not o["groups"]:
            res["reasons"].append("복사할 종류를 하나 이상 고르세요.")
        if not rows and o["groups"]:
            res["reasons"].append("복사할 파일이 없어요(꺼낸 폴더에 파일이 없을 수 있어요).")
        if not outp:
            res["reasons"].append("저장할 곳을 고르세요.")
            return res
        for _, p in pairs:
            if guards._under(outp, p):
                res["reasons"].append("꺼낸 폴더(%s) 안에는 저장할 수 없어요. 다른 곳을 고르세요." % p)
                break
        have = orgmod.present_bytes(rows, outp)
        chk = guards.check_dest(outp, self.forbidden_mounts(dev) if dev else [], res["bytes"] - have)
        res["free"], res["present"] = chk["free"], have
        res["reasons"] += [x.replace(" 묶음 크기를 줄이세요.", " 다른 곳을 고르거나 종류를 줄이세요.") for x in chk["reasons"]]
        res["ok"] = not res["reasons"]
        return res

    def organize_target(self, out, opts):
        def target(job):
            outp = os.path.realpath(expand(out))
            rows, cnt = orgmod.plan(self._org_items(True), outp, orgmod.opts_of(opts))
            job.set_phase("copy")

            def prog(p):
                if p["phase"] != job.phase:
                    job.set_phase(p["phase"])
                job.update(done=p["done"], total=p["total"], done_bytes=p["done_bytes"], total_bytes=p["total_bytes"])
            r = orgmod.run(rows, outp, on_progress=prog, should_cancel=job.cancel.is_set)
            chown_tree(outp)
            info = {k: r[k] for k in ("files", "bytes", "copied", "skipped", "checked", "finished")}
            info.update(mismatch=len(r["mismatch"]), mismatch_list=r["mismatch"][:50], dir=outp, opts=orgmod.opts_of(opts),
                        at=time.strftime("%Y-%m-%d %H:%M"))
            os.makedirs(self.session, exist_ok=True)
            with open(os.path.join(self.session, "organize.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
            job.finish("done" if r["finished"] else "cancelled", **{k: info[k] for k in ("files", "copied", "checked", "mismatch", "dir")})
        return target

    def add_image(self, path):
        p = os.path.realpath(expand(path))
        if not os.path.isfile(p):
            raise FileNotFoundError("이미지 파일이 없어요: %s" % path)
        if os.path.splitext(p)[1].lower() not in (".img", ".dmg", ".bin", ".raw", ".dd", ".iso"):
            raise ValueError("디스크 이미지 파일(.img 등)만 열 수 있어요.")
        name = "이미지 " + os.path.splitext(os.path.basename(p))[0]
        self.images[name] = p
        self._shared.pop("devs", None)
        return name

    def saved_dest(self):
        return compat.read_json(os.path.join(self.session, "plan.json"), {}).get("dest")

    def verify_overview(self):
        """열림 확인 결과. 이어진 옛 기록(같은 장치에서 먼저 꺼낸 것)의 결과도 합친다."""
        out, fails, fixed = None, [], []
        for c in self.related_sessions():
            vs = vf.load_summary(c)
            if vs:
                if out is None:
                    out = dict(vs, levels=dict(vs["levels"]), reasons=dict(vs["reasons"]), tools=dict(vs["tools"]))
                else:
                    for k in ("total", "ok", "fail", "fixed", "unchecked"):
                        out[k] += vs[k]
                    for k in ("levels", "reasons"):
                        for kk, n in vs[k].items():
                            out[k][kk] = out[k].get(kk, 0) + n
            fixed += vf.load_fixed(c)
        a = self.acks()
        fails = [dict(f, ack=a.get(f["path"])) for f in self.all_failures()]
        fails.sort(key=lambda f: bool(f["ack"]))  # 아직 안 본 것부터
        if out is not None:
            out["acked"] = sum(1 for f in fails if f["ack"])
            out["fail"] = max(0, out["fail"] - out["acked"])
        return {"summary": out, "failures": fails[:200], "fixed": fixed[:200]}

    def result_summary(self, n_samples=12):
        """결과 화면용: 꺼낸 파일의 종류별 개수, 용량, 촬영일 범위, 미리보기 후보."""
        chain = [c for c in self.related_sessions() if os.path.isfile(os.path.join(c, "extracted.jsonl"))]
        done = [r for c in chain for r in sorted(ex.load_extracted(c).values(), key=lambda r: r["id"])]  # 이어진 옛 기록에서 꺼낸 것도 합친다
        by_kind, total, suspect, days = {}, 0, 0, []
        for r in done:
            k = r["path"].replace("\\", "/").split("/")
            by_kind[k[0]] = by_kind.get(k[0], 0) + 1
            total += r.get("bytes", 0)
            if ex.SUSPECT_DIR in k:
                suspect += 1
            m = re.search(r"\d{4}-\d{2}-\d{2}", r["path"])
            if m:
                days.append(m.group(0))
        photos = [r for r in done if r.get("kind") == "jpeg"]
        pool = photos if len(photos) >= n_samples else photos + [r for r in done if r.get("kind") != "jpeg"]
        step = max(1, len(pool) // n_samples)
        samples = [{"path": r["path"], "kind": r.get("kind")} for r in pool[::step][:n_samples]]
        return {"files": len(done), "bytes": total, "by_kind": by_kind, "suspect": suspect,
                "duplicates": sum(len(ex.load_dups(c)) for c in chain), "dest": self.saved_dest(),
                "first_day": min(days) if days else None, "last_day": max(days) if days else None,
                "samples": samples}

    def report(self):
        home = real_home()
        info = compat.session_info(self.session)
        lines = ["APFS 복구 보고서 (개인정보는 가렸습니다)",
                 "[형식] 보고서 v%d · 기록 v%d%s" % (compat.REPORT_FORMAT, info["schema"],
                                               " (이전 버전 기록을 읽음)" if info["legacy"] else " (더 새 버전 기록: 일부만 읽음)" if info["newer"] else ""), ""]
        cat = os.path.join(self.session, "catalog.jsonl")
        if os.path.exists(cat):
            s = summarize(self.session)
            if s.get("skipped"):
                lines += ["[주의] 읽지 못해 건너뛴 검사 기록 %d줄" % s["skipped"]]
            lines += ["[검사 결과] 총 %d개, %.2f GB" % (s["total"], sum(s["bytes"].values()) / 1e9)]
            for k in sorted(s["kinds"]):
                st = s["structure"][k]
                lines.append("  %-14s %7d개 %8.2f GB  구조 확인됨 %d · 의심 %d" % (
                    k, s["kinds"][k], s["bytes"][k] / 1e9, st.get("ok", 0), st.get("suspect", 0)))
            lines.append("  촬영일 없음: %d개" % s["undated"])
        done = ex.load_extracted(self.session)
        rs = self.result_summary()
        lines += ["", "[복구] 꺼낸 파일 %d개 (%.2f GB)%s" % (rs["files"], rs["bytes"] / 1e9,
                  " — 같은 장치의 이전 기록 %d개 포함" % (len(self.related_sessions()) - 1) if len(self.related_sessions()) > 1 else "")]
        vo = self.verify_overview()
        vs = vo["summary"]
        if vs:
            lines += ["", "[검증] 전체 %d · 열림 확인 %d · 복원본 %d · 실패 %d · 미검사 %d" % (
                vs["total"], vs["ok"], vs.get("fixed", 0), vs["fail"], vs["unchecked"]),
                "  검증 수준: " + ", ".join("%s %s" % kv for kv in sorted(vs["levels"].items())),
                "  사용한 도구: " + (", ".join(k for k, v in vs["tools"].items() if v) or "없음(구조 확인만)")]
            if vs.get("fixed"):
                lines.append("  복원본: %d개 (목차가 없거나 끝이 잘린 ZIP 에서 온전한 항목만 모아 만든 새 파일, 원본은 그대로)" % vs["fixed"])
            for r in vo["fixed"][:50]:
                sv = r.get("salvage") or {}
                lines.append("  복원본: %s — 온전한 항목 %s개%s → %s" % (r.get("path", ""), sv.get("entries", "?"),
                                                                 ", 마지막 항목 하나는 잘려서 뺌" if sv.get("lost") else "", sv.get("path", "")))
            for r in vo["failures"][:50]:
                lines.append("  %s: %s — %s" % ("문제없음(직접 확인 %s)" % r["ack"] if r.get("ack") else "실패", r.get("path", ""), r.get("reason", "")))
        tc = tailcheck.load(self.session)
        if tc:
            v = tailcheck.verdict(tc)
            step = "%d MB" % (tc["step"] >> 20) if tc["step"] >= 1 << 20 else "%d KB" % (tc["step"] >> 10)
            where = sorted(int(k) for k in tc.get("buckets", {}))
            lines += ["", "[끝부분 촘촘 검사] %.1f GB ~ %.1f GB 를 %s 마다 확인 · 찍어 본 곳 %d · 내용 있음 %d · 파일 시작 표시 %d%s" % (
                tc["start"] / 1e9, tc["end"] / 1e9, step, tc["samples"], tc["nonzero"], len(tc["hits"]),
                "" if tc.get("finished") else " (중간에 멈춤)")]
            if where:
                lines.append("  내용이 있는 위치: " + ", ".join("%d~%d GB" % (k, k + 1) for k in where[:20]) + (" 외" if len(where) > 20 else ""))
            for hit in tc["hits"][:20]:
                lines.append("  파일 시작 표시: %.2f GB 지점, 종류 %s" % (hit["offset"] / 1e9, hit["kind"]))
            lines += [
                "  결론: " + {"empty": "뒤쪽은 비어 있음(추정)", "residue": "내용은 있으나 파일 시작은 못 찾음(지워진 찌꺼기일 수 있음)",
                             "files": "파일 시작 표시가 있음 — 그 구간 검사를 권함", "partial": "끝까지 확인하지 않음"}[v]]
        lines += ["", "[알려진 한계]",
                  "  - 원래 파일명·폴더 구조는 복구하지 못합니다.",
                  "  - 지워진 옛 파일이 섞였는지 구분할 수 없습니다.",
                  "  - 'structure' 수준 검증은 구조가 맞다는 뜻이며 재생·현상 프로그램에서의 열림은 확인하지 않았습니다."]
        return re.sub(re.escape(home), "~", "\n".join(lines))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "apfsrecover"

        def log_message(self, *a):  # 콘솔을 조용히
            pass

        # ---- 공통 ----
        def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")  # blob: 크게 보기 그림(오류 문구를 읽으려고 fetch 로 받는다)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, {"error": msg})

        def _authorized(self, query):
            host = (self.headers.get("Host") or "").lower()
            if host not in ("127.0.0.1:%d" % app.port, "localhost:%d" % app.port):
                self._err(403, "허용되지 않은 Host")
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://127.0.0.1:%d" % app.port, "http://localhost:%d" % app.port):
                self._err(403, "허용되지 않은 Origin")
                return False
            tok = self.headers.get("X-Token") or (query.get("token") or [""])[0]
            if not secrets.compare_digest(tok, app.token):
                self._err(401, "토큰이 필요합니다. 서버가 출력한 주소로 접속하세요.")
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("요청이 너무 큼")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        # ---- 라우팅 ----
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/favicon.ico":  # 토큰 없이 오는 브라우저 자동 요청: 오류 대신 빈 응답
                self.send_response(204)
                self.end_headers()
                return
            if not self._authorized(q):
                return
            try:
                if u.path == "/":
                    with open(UI_PATH, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/devices":  # fresh=1: 다시 꽂혔는지 확인할 때(목록·지문을 새로 읽는다)
                    fresh = (q.get("fresh") or ["0"])[0] == "1"
                    devs = app._devices_cached(fresh)
                    dj = app.session_device()
                    for d in devs:
                        d["matches_session"] = bool(dj and dj.get("fingerprint") and d["readable"]
                                                    and (app.fingerprint(d) if fresh else app._fp_cached(d)) == dj["fingerprint"])
                    return self._send(200, {"devices": devs, "images": bool(app.images),
                                            "is_root": is_admin()})
                if u.path == "/api/session":
                    has = os.path.exists(os.path.join(app.session, "catalog.jsonl"))
                    dev = compat.read_json(os.path.join(app.session, "device.json"), None)
                    return self._send(200, {"session": app.session.replace(real_home(), "~"),
                                            "has_catalog": has, "device": dev,
                                            "dest": app.saved_dest(), "tools": vf.available_tools(),
                                            "interrupted": app.interrupted(), "on_battery": on_battery(),
                                            "compat": compat.session_info(app.session)})
                if u.path == "/api/sessions/reusable":
                    dev = app.find_device((q.get("device_id") or [""])[0])
                    return self._send(200, {"sessions": [{k: v for k, v in c.items() if k != "path"} for c in app.reusable(dev)] if dev else []})
                if u.path in ("/api/fs/places", "/api/fs/list"):
                    dev = app.find_device((q.get("device_id") or [""])[0]) if q.get("device_id") else None
                    forb = app.forbidden_mounts(dev)
                    if u.path == "/api/fs/places":
                        return self._send(200, {"places": fsbrowse.places(forb)})
                    try:
                        return self._send(200, fsbrowse.list_dir((q.get("path") or [""])[0], forb))
                    except FileNotFoundError as e:
                        return self._err(404, str(e))
                if u.path == "/api/job":
                    return self._send(200, {"job": app.job.to_dict() if app.job else None})
                if u.path == "/api/catalog/summary":
                    if not os.path.exists(os.path.join(app.session, "catalog.jsonl")):
                        return self._err(404, "검사 결과가 없습니다.")
                    return self._send(200, summarize(app.session))
                if u.path == "/api/catalog/recent":  # 검사 중 "방금 찾은 사진" 줄
                    return self._send(200, {"items": recent_thumbs(app.session)})
                if u.path == "/api/catalog/items":  # 꺼내기 전 탐색 화면: 찾은 파일 전체(짧은 형태)
                    items = browse_items(app.session)
                    if items is None:
                        return self._err(404, "검사 결과가 없습니다.")
                    return self._send(200, items)
                if u.path == "/api/preview":  # 찾은 파일 하나를 원본에서 읽어 크게 보기(읽기만)
                    dev = app.find_device((q.get("device_id") or [""])[0])
                    if not dev:
                        return self._err(400, "목록에 없는 장치입니다. 장치를 연결한 뒤 다시 하세요.")
                    try:
                        rid = int((q.get("id") or [""])[0])
                    except ValueError:
                        return self._err(404, "검사 기록에 없는 파일입니다.")
                    try:
                        return self._send(200, app.preview(dev, rid), "image/jpeg")
                    except PreviewError as e:
                        return self._err(e.code, str(e))
                if u.path == "/api/verify/summary":
                    return self._send(200, app.verify_overview())
                if u.path == "/api/units":
                    return self._send(200, {"units": app.units(fresh=(q.get("fresh") or ["0"])[0] == "1"), "images": bool(app.images), "is_root": is_admin(),
                                            "current": os.path.basename(app.session),
                                            "root": app.root.replace(real_home(), "~"), "all_disks": app.all_disks})
                if u.path == "/api/format-check":
                    dev = app.find_device((q.get("device_id") or [""])[0]) if q.get("device_id") else None
                    return self._send(200, app.format_check(dev))
                if u.path == "/api/health":
                    dev = app.find_device((q.get("device_id") or [""])[0])
                    return self._send(200, app.health_of(dev) if dev else healthmod.health(None))
                if u.path == "/api/backup":
                    return self._send(200, app.backup_info())
                if u.path == "/api/partitions":
                    return self._send(200, {"result": compat.read_json(os.path.join(app.session, "partitions.json"), None)})
                if u.path == "/api/organize":
                    return self._send(200, {"last": compat.read_json(os.path.join(app.session, "organize.json"), None)})
                if u.path == "/api/names":
                    return self._send(200, {"result": compat.read_json(os.path.join(app.session, "names.json"), None)})
                if u.path == "/api/tail":
                    return self._send(200, app.tail_info())
                if u.path == "/api/report":
                    return self._send(200, app.report().encode("utf-8"), "text/plain; charset=utf-8")
                if u.path == "/api/result":
                    return self._send(200, app.result_summary())
                if u.path == "/api/recent":
                    n = max(1, min(24, int((q.get("n") or ["8"])[0])))
                    recs = _tail_jsonl(os.path.join(app.session, "extracted.jsonl"), n)
                    return self._send(200, {"items": [{"path": r["path"], "kind": r.get("kind")}
                                                      for r in recs if not r.get("invalid") and r.get("path")]})
                if u.path == "/api/catalog/thumb":  # 검사 때 저장한 내장 미리보기(세션 폴더의 thumbs/<번호>.jpg 만)
                    try:
                        rid = int((q.get("id") or [""])[0])
                    except ValueError:
                        return self._err(404, "없는 미리보기")
                    p = thumb_path(app.session, rid) if rid >= 0 and app.session else None
                    if not p or not os.path.isfile(p):
                        return self._err(404, "없는 미리보기")
                    with open(p, "rb") as f:  # 주소에 기록 버전(v)이 붙으므로 잠깐 캐시해도 옛 그림이 나오지 않는다
                        return self._send(200, f.read(), "image/jpeg", cache="private, max-age=3600")
                if u.path == "/api/thumb":
                    dest = app.saved_dest()
                    rel = (q.get("p") or [""])[0]
                    if not dest or not rel:
                        return self._err(404, "없는 파일")
                    full = os.path.join(os.path.realpath(expand(dest)), rel)
                    if not _inside(full, expand(dest)) or not os.path.isfile(full):
                        return self._err(404, "없는 파일")  # 저장 폴더 밖은 절대 읽지 않는다
                    data = thumbnail(full)
                    if data is None:
                        return self._err(404, "미리보기를 만들 수 없습니다")
                    return self._send(200, data, "image/jpeg")
                return self._err(404, "없는 경로")
            except Exception as e:
                return self._err(500, "%s: %s" % (type(e).__name__, e))

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authorized(parse_qs(u.query)):
                return
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._err(400, "잘못된 요청: %s" % e)
            try:
                return self._post(u.path, body)
            except Exception as e:
                return self._err(500, "%s: %s" % (type(e).__name__, e))

        def _device(self, body):
            dev = app.find_device(body.get("device_id"))
            if not dev:
                self._err(400, "목록에 없는 장치입니다. 새로고침 후 다시 선택하세요.")
            return dev

        def _post(self, path, body):
            if path == "/api/scan/start":
                dev = self._device(body)
                if not dev:
                    return
                if not dev["readable"]:
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한으로 다시 실행하세요(맥: RecoveryTool.command, 윈도우: RecoveryTool.bat).")
                reuse = None
                if body.get("reuse") and not body.get("resume"):  # 목록에 있는(같은 디스크로 확인된) 이전 검사만 쓴다
                    reuse = next((c["path"] for c in app.reusable(dev) if c["name"] == body["reuse"]), None)
                    if reuse is None:
                        return self._err(400, "선택한 이전 검사 기록을 쓸 수 없습니다(같은 디스크가 아니거나 없어졌습니다).")
                job = app.start_job("scan", app.view().scan_target(dev, bool(body.get("full")), bool(body.get("resume")), reuse))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/auto/start":
                dev = self._device(body)
                if not dev:
                    return
                if not dev["readable"]:
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한으로 다시 실행하세요(맥: RecoveryTool.command, 윈도우: RecoveryTool.bat).")
                plan = body.get("plan") or {}
                if not plan.get("kinds"):
                    return self._err(400, "복구할 종류를 하나 이상 고르세요.")
                early = guards.check_dest(plan.get("dest"), app.forbidden_mounts(dev), 0)
                if not early["ok"]:
                    return self._err(400, " / ".join(early["reasons"]))
                job = app.start_job("auto", app.view().auto_target(dev, plan, bool(body.get("full")), bool(body.get("resume"))))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/verify/ack":  # 확인 필요 파일을 '문제없음'으로 표시하거나 되돌린다(기록 폴더의 ack.json 에만 쓴다)
                paths = body.get("paths") or []
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    return self._err(400, "잘못된 요청입니다.")
                try:
                    app.set_ack(paths, bool(body.get("ack", True)))
                except ValueError as e:
                    return self._err(400, str(e))
                return self._send(200, app.verify_overview())
            if path == "/api/open-file":  # 꺼낸 파일 하나를 연결된 프로그램으로 연다(저장 폴더 안의 파일만)
                dest = app.saved_dest()
                rel = body.get("path") or ""
                if not dest or not isinstance(rel, str):
                    return self._err(404, "저장 폴더를 찾을 수 없습니다.")
                base = os.path.realpath(expand(dest))
                p = os.path.realpath(os.path.join(base, rel))
                if not guards._under(p, base) or not os.path.isfile(p):
                    return self._err(404, "파일이 저장 폴더에 없어요.")
                open_folder(p)
                return self._send(200, {"ok": True})
            if path == "/api/open-folder":
                dest = app.saved_dest()
                if not dest or not os.path.isdir(os.path.realpath(expand(dest))):
                    return self._err(404, "저장 폴더를 찾을 수 없습니다.")
                open_folder(os.path.realpath(expand(dest)))
                return self._send(200, {"ok": True})
            if path == "/api/report/save":
                dest = app.saved_dest()
                if not dest or not os.path.isdir(os.path.realpath(expand(dest))):
                    return self._err(404, "저장 폴더를 찾을 수 없습니다.")
                out = os.path.join(os.path.realpath(expand(dest)), "복구 보고서.txt")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(app.report())
                chown_tree(out)
                return self._send(200, {"path": out})
            if path == "/api/shutdown":
                if app.job and app.job.state == "running":
                    return self._err(409, "작업이 실행 중입니다. 끝나거나 중단한 뒤 종료하세요.")
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/fs/pick":  # 운영체제 기본 폴더 선택 창
                from . import nativepick
                try:
                    return self._send(200, {"path": nativepick.pick_folder(body.get("path"))})
                except nativepick.NativePickUnavailable:
                    return self._err(501, "이 환경에서는 기본 폴더 선택 창을 쓸 수 없습니다.")
            if path == "/api/fs/mkdir":
                dev = app.find_device(body.get("device_id")) if body.get("device_id") else None
                try:
                    made = fsbrowse.make_dir(body.get("parent"), body.get("name"), app.forbidden_mounts(dev))
                except ValueError as e:
                    return self._err(400, str(e))
                return self._send(200, {"path": made})
            if path == "/api/job/cancel":
                if app.job and app.job.state == "running":
                    app.job.cancel.set()
                return self._send(200, {"ok": True})
            if path == "/api/plan/check":
                if not os.path.exists(os.path.join(app.session, "catalog.jsonl")):
                    return self._err(404, "검사 결과가 없습니다.")
                dev = app.find_device(body.get("device_id")) if body.get("device_id") else None
                return self._send(200, ex.check_plan(app.session, body.get("plan") or {},
                                                     app.forbidden_mounts(dev)))
            if path == "/api/extract/start":
                dev = self._device(body)
                if not dev:
                    return
                plan = body.get("plan") or {}
                bad = app.disk_mismatch(dev)
                if bad:
                    return self._err(400, bad)
                chk = ex.check_plan(app.session, plan, app.forbidden_mounts(dev))
                if not chk["ok"]:
                    return self._err(400, " / ".join(chk["reasons"]))
                job = app.start_job("extract", app.view().extract_target(dev, plan))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/select":  # 장치 목록에서 고른 장치의 기록 폴더로 바꾼다(실행 중인 작업은 자기 기록 폴더를 그대로 쓴다)
                p = app.unit_session(body.get("key") or "")
                if not p:
                    return self._err(404, "목록에 없는 장치입니다. 목록을 새로 고친 뒤 다시 고르세요.")
                app.session = os.path.realpath(p)
                return self._send(200, {"session": os.path.basename(app.session)})
            if path == "/api/prefs":
                if "all_disks" in body:
                    app.all_disks = bool(body["all_disks"])
                return self._send(200, {"all_disks": app.all_disks})
            if path in ("/api/backup/start", "/api/partitions/start", "/api/names/start"):
                dev = self._device(body)
                if not dev:
                    return
                if not dev["readable"]:
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한으로 다시 실행하세요.")
                v = app.view()
                if path == "/api/backup/start":
                    if not body.get("resume") and not body.get("dest"):
                        return self._err(400, "이미지를 저장할 폴더를 고르세요.")
                    if body.get("dest"):
                        early = guards.check_dest(body["dest"], app.forbidden_mounts(dev), 0)
                        if not early["ok"]:
                            return self._err(400, " / ".join(early["reasons"]))
                    job = app.start_job("backup", v.backup_target(dev, body.get("dest") or "", bool(body.get("whole")), bool(body.get("resume"))))
                elif path == "/api/partitions/start":
                    job = app.start_job("partfind", v.partfind_target(dev))
                else:
                    bad = app.disk_mismatch(dev)
                    if bad:
                        return self._err(400, bad)
                    job = app.start_job("names", v.names_target(dev))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/organize/open":
                last = compat.read_json(os.path.join(app.session, "organize.json"), None)
                if not last or not os.path.isdir(last.get("dir") or ""):
                    return self._err(404, "정리한 폴더를 찾을 수 없어요.")
                open_folder(last["dir"])
                return self._send(200, {"ok": True})
            if path in ("/api/organize/check", "/api/organize/start"):  # 꺼낸 파일을 정리해서 복사(원본 장치는 읽지 않는다)
                dev = app.find_device(body["device_id"]) if body.get("device_id") else None
                chk = app.organize_check(body.get("out") or "", body.get("opts") or {}, dev, fresh=path.endswith("start"))
                if path.endswith("check"):
                    return self._send(200, chk)
                if not chk["ok"]:
                    return self._err(400, " / ".join(chk["reasons"]))
                job = app.start_job("organize", app.view().organize_target(body["out"], body.get("opts") or {}))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/images/open":
                try:
                    name = app.add_image(body.get("path") or "")
                except (OSError, ValueError) as e:
                    return self._err(400, str(e))
                return self._send(200, {"name": name, "key": "dev:image:" + name})
            if path == "/api/tail/start":
                dev = self._device(body)
                if not dev:
                    return
                if not dev["readable"]:
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한으로 다시 실행하세요.")
                info = app.tail_info()
                if not info["can"]:
                    return self._err(400, info["why"])
                bad = app.disk_mismatch(dev)
                if bad:
                    return self._err(400, bad)
                job = app.start_job("tail", app.view().tail_target(dev, bool(body.get("resume"))))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/verify/start":
                dest = body.get("dest") or app.saved_dest()
                if not dest:
                    return self._err(400, "검증할 저장 폴더를 알 수 없습니다. 먼저 복구를 실행하세요.")
                job = app.start_job("verify", app.view().verify_target(os.path.realpath(expand(dest)),
                                                                body.get("deep", True)))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            return self._err(404, "없는 경로")
    return Handler


def serve(app, port=0, open_browser=True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/?token=%s" % (app.port, app.token)
    return httpd, url
