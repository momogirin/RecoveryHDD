"""운영체제 기본 '폴더 선택' 창을 띄운다(윈도우는 탐색기 창). 사용할 수 없으면 NativePickUnavailable."""
import os


class NativePickUnavailable(Exception):
    pass


def pick_folder(initial=None, title="저장할 폴더 선택"):
    """고른 폴더 경로를 돌려준다. 취소하면 None. 창이 닫힐 때까지 기다린다(호출한 스레드에서 실행)."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as e:  # tkinter 가 없는 환경
        raise NativePickUnavailable(str(e))
    try:
        root = tkinter.Tk()
    except Exception as e:  # 화면이 없는 환경 등
        raise NativePickUnavailable(str(e))
    try:
        root.withdraw()
        root.attributes("-topmost", True)  # 전용 창 뒤에 숨지 않게
        start = initial if initial and os.path.isdir(initial) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(parent=root, initialdir=start, title=title, mustexist=True)
    finally:
        root.destroy()
    return os.path.normpath(chosen) if chosen else None
