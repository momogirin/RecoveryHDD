"""P2: 로컬 웹 서버 (127.0.0.1 전용, 토큰 필요) + 작업 관리.

디스크에 쓰는 엔드포인트는 존재하지 않는다. 장치는 목록(/api/devices)에 있는 것만 선택할 수 있고,
임의 경로를 장치로 받지 않는다. 저장 폴더는 guards.check_dest 를 통과해야 한다.
"""
import errno
import json
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import devices as devmod
from . import extract as ex
from . import guards
from . import verify as vf
from .blockdev import ReadOnlyDevice
from .catalog import BS, catalog, summarize
from .ownership import chown_tree, expand, real_home
from .survey import estimate_extent

UI_PATH = os.path.join(os.path.dirname(__file__), "ui", "index.html")
MARGIN_AFTER_DATA = 2 * 1000**3
MAX_BODY = 1 << 20


class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
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
            return {"id": self.id, "kind": self.kind, "state": self.state, "phase": self.phase,
                    "progress": p, "error": self.error, "result": self.result,
                    "elapsed_s": int((self.finished or time.time()) - self.started)}


class App:
    def __init__(self, session, images=None, sample_step=128 * 1024 * 1024, token=None):
        self.session = os.path.realpath(expand(session))
        self.images = dict(images or {})
        self.sample_step = sample_step
        self.token = token or secrets.token_urlsafe(24)
        self.job = None
        self.lock = threading.Lock()
        self.port = None

    # ---- 장치 ----
    def devices(self):
        return devmod.list_devices(self.images)

    def find_device(self, device_id):
        for d in self.devices():
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
            for d in self.devices():
                if d.get("whole_disk") == whole:
                    out += d["mounts"]
        return out

    # ---- 작업 ----
    def start_job(self, kind, target):
        with self.lock:
            if self.job and self.job.state == "running":
                return None
            job = Job(kind)
            self.job = job

        def run():
            try:
                target(job)
            except PermissionError:
                job.finish("error", "이 장치를 읽을 권한이 없습니다. 관리자 권한(sudo)으로 다시 실행하세요.")
            except OSError as e:
                if e.errno in (errno.ENXIO, errno.ENODEV):
                    job.finish("disconnected", "장치 연결이 끊겼습니다. 원본은 읽기만 했으므로 안전합니다. "
                               "케이블을 다시 꽂고 장치를 다시 선택한 뒤 이어서 하세요.")
                else:
                    job.finish("error", "%s" % e)
            except Exception as e:  # 작업 스레드가 조용히 죽지 않게 한다
                job.finish("error", "%s: %s" % (type(e).__name__, e))
            finally:
                chown_tree(self.session)
                if job.result.get("dest"):
                    chown_tree(job.result["dest"])
        threading.Thread(target=run, daemon=True).start()
        return job

    DISCONNECT_MSG = ("장치 연결이 끊겼습니다. 원본은 읽기만 했으므로 안전합니다. "
                      "케이블을 다시 꽂고 장치를 다시 선택한 뒤 이어서 하세요.")

    def _scan(self, job, dev, full, resume):
        """검사 한 번. 반환: (state, info) — state: done | nodata | cancelled | disconnected"""
        size = dev["size"]
        end_bytes = size
        prev = None
        djp = os.path.join(self.session, "device.json")
        if resume and os.path.exists(djp):
            with open(djp, encoding="utf-8") as f:
                prev = json.load(f)
            if prev.get("size") == size and prev.get("scanned_bytes") and not prev.get("finished"):
                end_bytes = prev["scanned_bytes"]
                full = True  # 샘플링을 다시 하지 않고 이전 범위로 이어서 검사
            else:
                prev = None
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
        rc = catalog(dev["path"], self.session, 0, end_bytes // BS,
                     on_progress=lambda p: job.update(done_bytes=p["pos"], total_bytes=p["total"], found=p["found"]),
                     should_cancel=job.cancel.is_set, fresh=not resume)
        with open(os.path.join(self.session, "device.json"), "w", encoding="utf-8") as f:
            json.dump({"device": dev["id"], "size": size, "scanned_bytes": end_bytes,
                       "full": bool(full) and prev is None, "finished": rc == 0}, f)
        return {0: "done", 3: "cancelled"}.get(rc, "disconnected"), {"end_bytes": end_bytes}

    def scan_target(self, dev, full, resume):
        def target(job):
            state, info = self._scan(job, dev, full, resume)
            if state == "done":
                job.finish("done", summary=summarize(self.session), **info)
            elif state == "nodata":
                job.finish("done", error="디스크에서 데이터를 찾지 못했습니다(샘플링 기준).", found=0, end_bytes=0)
            elif state == "disconnected":
                job.finish("disconnected", self.DISCONNECT_MSG)
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
                                  files=r["files"], bytes=r["bytes"], dest=dest, catalog=summ)
            job.set_phase("verifying")
            v = vf.verify_session(self.session, dest,
                                  on_progress=lambda p: job.update(done=p["done"], total=p["total"], ok=p["ok"], fail=p["fail"]),
                                  should_cancel=job.cancel.is_set, deep=True)
            job.finish("cancelled" if v["cancelled"] else "done", files=r["files"], bytes=r["bytes"], dest=dest,
                       verify=v, catalog=summ)
        return target

    def extract_target(self, dev, plan):
        def target(job):
            job.set_phase("extracting")
            r = ex.extract_batch(
                dev["path"], self.session, plan, forbidden=self.forbidden_mounts(dev),
                on_progress=lambda p: job.update(done_bytes=p["bytes"], total_bytes=p["total_bytes"],
                                                 files=p["files"], total_files=p["total_files"],
                                                 current=p["current"]),
                should_cancel=job.cancel.is_set)
            dest = os.path.realpath(expand(plan["dest"])) if r["state"] != "refused" else None
            job.finish(r["state"], r["error"], files=r["files"], bytes=r["bytes"], dest=dest)
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

    def saved_dest(self):
        p = os.path.join(self.session, "plan.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f).get("dest")
        return None

    def report(self):
        home = real_home()
        lines = ["APFS 복구 보고서 (개인정보는 가렸습니다)", ""]
        cat = os.path.join(self.session, "catalog.jsonl")
        if os.path.exists(cat):
            s = summarize(self.session)
            lines += ["[검사 결과] 총 %d개, %.2f GB" % (s["total"], sum(s["bytes"].values()) / 1e9)]
            for k in sorted(s["kinds"]):
                st = s["structure"][k]
                lines.append("  %-14s %7d개 %8.2f GB  구조 확인됨 %d · 의심 %d" % (
                    k, s["kinds"][k], s["bytes"][k] / 1e9, st.get("ok", 0), st.get("suspect", 0)))
            lines.append("  촬영일 없음: %d개" % s["undated"])
        done = ex.load_extracted(self.session)
        lines += ["", "[복구] 꺼낸 파일 %d개 (%.2f GB)" % (len(done), sum(r["bytes"] for r in done.values()) / 1e9)]
        vs = vf.load_summary(self.session)
        if vs:
            lines += ["", "[검증] 전체 %d · 열림 확인 %d · 실패 %d · 미검사 %d" % (
                vs["total"], vs["ok"], vs["fail"], vs["unchecked"]),
                "  검증 수준: " + ", ".join("%s %d" % kv for kv in sorted(vs["levels"].items())),
                "  사용한 도구: " + (", ".join(k for k, v in vs["tools"].items() if v) or "없음(구조 확인만)")]
            for r in vf.load_failures(self.session, 50):
                lines.append("  실패: %s — %s" % (r["path"], r["reason"]))
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
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
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
                if u.path == "/api/devices":
                    return self._send(200, {"devices": app.devices(), "images": bool(app.images),
                                            "is_root": hasattr(os, "geteuid") and os.geteuid() == 0})
                if u.path == "/api/session":
                    has = os.path.exists(os.path.join(app.session, "catalog.jsonl"))
                    dev = None
                    p = os.path.join(app.session, "device.json")
                    if os.path.exists(p):
                        with open(p, encoding="utf-8") as f:
                            dev = json.load(f)
                    return self._send(200, {"session": app.session.replace(real_home(), "~"),
                                            "has_catalog": has, "device": dev,
                                            "dest": app.saved_dest(), "tools": vf.available_tools()})
                if u.path == "/api/job":
                    return self._send(200, {"job": app.job.to_dict() if app.job else None})
                if u.path == "/api/catalog/summary":
                    if not os.path.exists(os.path.join(app.session, "catalog.jsonl")):
                        return self._err(404, "검사 결과가 없습니다.")
                    return self._send(200, summarize(app.session))
                if u.path == "/api/verify/summary":
                    return self._send(200, {"summary": vf.load_summary(app.session),
                                            "failures": vf.load_failures(app.session)})
                if u.path == "/api/report":
                    return self._send(200, app.report().encode("utf-8"), "text/plain; charset=utf-8")
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
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한(sudo)으로 다시 실행하세요.")
                job = app.start_job("scan", app.scan_target(dev, bool(body.get("full")), bool(body.get("resume"))))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/auto/start":
                dev = self._device(body)
                if not dev:
                    return
                if not dev["readable"]:
                    return self._err(403, "이 장치를 읽을 권한이 없습니다. 관리자 권한(sudo)으로 다시 실행하세요.")
                plan = body.get("plan") or {}
                if not plan.get("kinds"):
                    return self._err(400, "복구할 종류를 하나 이상 고르세요.")
                early = guards.check_dest(plan.get("dest"), app.forbidden_mounts(dev), 0)
                if not early["ok"]:
                    return self._err(400, " / ".join(early["reasons"]))
                job = app.start_job("auto", app.auto_target(dev, plan, bool(body.get("full")), bool(body.get("resume"))))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/shutdown":
                if app.job and app.job.state == "running":
                    return self._err(409, "작업이 실행 중입니다. 끝나거나 중단한 뒤 종료하세요.")
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
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
                chk = ex.check_plan(app.session, plan, app.forbidden_mounts(dev))
                if not chk["ok"]:
                    return self._err(400, " / ".join(chk["reasons"]))
                job = app.start_job("extract", app.extract_target(dev, plan))
                return self._send(200, {"job": job.to_dict()}) if job else self._err(409, "다른 작업이 실행 중입니다.")
            if path == "/api/verify/start":
                dest = body.get("dest") or app.saved_dest()
                if not dest:
                    return self._err(400, "검증할 저장 폴더를 알 수 없습니다. 먼저 복구를 실행하세요.")
                job = app.start_job("verify", app.verify_target(os.path.realpath(expand(dest)),
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
