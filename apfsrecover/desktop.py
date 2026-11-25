"""창이 있는 프로그램(.exe)의 시작점.

화면(웹 UI)은 그대로 쓰되 브라우저가 아니라 전용 창(WebView2)에 띄운다. 서버는 이 컴퓨터(127.0.0.1)에서만 열리고
실행마다 새 토큰을 쓴다(`server.py`). 창을 닫으면 도구도 끝난다. 작업이 실행 중이면 닫지 못하게 막는다.
전용 창을 쓸 수 없으면(WebView2 없음 등) 기본 브라우저로 연다.
"""
import argparse
import os
import sys
import threading
import webbrowser

TITLE = "사진 복구 도구"
BUSY_MSG = "작업이 실행 중입니다. 끝나거나 [중단]한 뒤에 닫으세요. (원본 디스크는 읽기만 하므로 안전합니다)"


def _args(argv):
    ap = argparse.ArgumentParser(prog="RecoveryTool")
    ap.add_argument("--session", default="~/Documents/recovered/session")
    ap.add_argument("--image", action="append", default=[], metavar="이름=경로",
                    help="시연/테스트용 디스크 이미지 파일 등록(읽기 전용)")
    ap.add_argument("--all-disks", action="store_true", help="내장 버스(NVMe/SATA) 디스크도 표시")
    ap.add_argument("--browser", action="store_true", help="전용 창 대신 기본 브라우저로 열기")
    ap.add_argument("--no-open", action="store_true", help="창도 브라우저도 열지 않음(시험용)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--url-file", help="시작 주소를 이 파일에 적는다(진단/시험용)")
    return ap.parse_args(argv)


def _message(title, text):
    """창이 없을 때(콘솔 없는 exe)도 오류가 보이도록 메시지 상자를 띄운다."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except (AttributeError, OSError):
        print(text, file=sys.stderr)


def main(argv):
    from . import lock
    from .ownership import expand
    from .privilege import is_admin, keep_awake
    from .server import App, serve

    a = _args(argv)
    images = {}
    for item in a.image:
        name, _, path = item.partition("=")
        images[name] = os.path.realpath(expand(path))
    app = App(a.session, images=images, sample_step=(64 * 1024 if images else 128 * 1024 * 1024),
              tail_step=(16 * 1024 if images else 32 * 1024 * 1024))
    app.all_disks = a.all_disks
    try:
        lockfile = lock.acquire(app.session)
    except RuntimeError as e:
        _message(TITLE, str(e))
        return 1
    httpd, url = serve(app, a.port)
    keep_awake()  # 작업 중 잠자기 방지
    print(url, flush=True)
    if a.url_file:  # 콘솔이 없는 exe 에서는 출력이 안 보이므로 파일로도 남긴다
        with open(a.url_file, "w", encoding="utf-8") as f:
            f.write(url)
    if not is_admin() and not images:
        print("관리자 권한이 아니라서 원본 디스크를 읽지 못할 수 있습니다.", file=sys.stderr, flush=True)

    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()
    try:
        if a.no_open:
            server.join()
        elif a.browser or not _open_window(app, httpd, url, server):
            webbrowser.open(url)
            server.join()  # 화면의 [도구 종료] 버튼이 서버를 멈춘다
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        lock.release(lockfile)
    return 0


def _open_window(app, httpd, url, server):
    """전용 창을 연다. 창을 쓸 수 없으면 False(호출한 쪽이 브라우저로 대신 연다)."""
    try:
        import webview
    except ImportError:
        return False
    window = webview.create_window(TITLE, url, width=1200, height=860, min_size=(900, 640))

    def on_closing():
        if app.job and app.job.state == "running":
            try:
                window.evaluate_js("alert(%r)" % BUSY_MSG)
            except Exception:  # 창 쪽 오류는 무시하고 닫기만 막는다
                pass
            return False
        return True

    def watch_server():  # 화면의 [도구 종료] 로 서버가 멈추면 창도 닫는다
        server.join()
        try:
            window.destroy()
        except Exception:
            pass

    window.events.closing += on_closing
    window.events.loaded += lambda: window.evaluate_js("document.documentElement.classList.add('native')")
    threading.Thread(target=watch_server, daemon=True).start()
    try:
        webview.start()
    except Exception:  # WebView2 런타임이 없는 등
        return False
    return True
