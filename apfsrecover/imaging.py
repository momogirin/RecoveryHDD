"""디스크 백업: 장치의 [start, end) 구간을 이미지 파일로 복사한다(원본은 읽기만 한다).

- 8MB 씩 읽고, 읽기 오류가 나면 그 8MB 를 64KB 단위로 다시 읽는다. 그래도 안 되는 64KB 는 0 으로 채우고
  '못 읽은 구간'으로 기록한다(멈추지 않고 넘어간다).
- 진행 상태는 이미지 옆의 <이미지>.json 에 남겨서 멈췄다가 이어서 할 수 있다.
- 만든 이미지는 이 프로그램에서 장치처럼 열어 검사·추출할 수 있다(이미지 파일도 읽기만 한다).
"""
import json
import os

from .blockdev import ReadOnlyDevice, device_gone

CHUNK = 8 << 20
SUB = 64 << 10
TRIES = 2


def state_path(image):
    return image + ".json"


def load_state(image):
    try:
        with open(state_path(image), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) and isinstance(st.get("pos"), int) else None
    except (OSError, ValueError):
        return None


def _save(image, st):
    tmp = state_path(image) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, state_path(image))


def _read(dev, off, n):
    last = None
    for _ in range(TRIES):
        try:
            b = dev.read_at(off, n)
            if len(b) == n:
                return b
            last = OSError("짧게 읽힘")
        except OSError as e:
            if device_gone(e):
                raise
            last = e
    raise last or OSError("읽기 실패")


def backup(device_path, image, start, end, fingerprint=None, resume=False, on_progress=None, should_cancel=None, dev_factory=ReadOnlyDevice):
    """반환: 상태 dict {"start","end","pos","bad":[[시작, 길이], ...],"bad_bytes","finished","image"}"""
    prev = load_state(image) if resume else None
    if prev and (prev.get("start"), prev.get("end"), prev.get("fingerprint")) == (start, end, fingerprint) and not prev.get("finished") \
            and os.path.isfile(image) and os.path.getsize(image) >= prev["pos"] - start:
        st = prev
    else:
        st = {"start": start, "end": end, "pos": start, "bad": [], "bad_bytes": 0, "finished": False, "fingerprint": fingerprint, "image": image}
        os.makedirs(os.path.dirname(os.path.abspath(image)), exist_ok=True)
        with open(image, "wb"):
            pass
    mode = "r+b"
    with dev_factory(device_path) as dev, open(image, mode) as out:
        out.seek(st["pos"] - start)
        n = 0
        while st["pos"] < end:
            if should_cancel and should_cancel():
                break
            off = st["pos"]
            size = min(CHUNK, end - off)
            try:
                data = _read(dev, off, size)
            except OSError as e:
                if device_gone(e):
                    out.flush()
                    _save(image, st)
                    raise
                parts = []
                for so in range(off, off + size, SUB):
                    sl = min(SUB, off + size - so)
                    try:
                        parts.append(_read(dev, so, sl))
                    except OSError as e2:
                        if device_gone(e2):
                            out.flush()
                            _save(image, st)
                            raise
                        parts.append(b"\0" * sl)  # 못 읽은 곳은 0 으로 채우고 기록한다
                        if st["bad"] and st["bad"][-1][0] + st["bad"][-1][1] == so:
                            st["bad"][-1][1] += sl
                        else:
                            st["bad"].append([so, sl])
                        st["bad_bytes"] += sl
                data = b"".join(parts)
            out.write(data)
            st["pos"] = off + size
            n += 1
            if n % 8 == 0:
                out.flush()
                _save(image, st)
            if on_progress:
                on_progress({"pos": st["pos"] - start, "total": end - start, "bad_bytes": st["bad_bytes"]})
        out.flush()
        os.fsync(out.fileno())
    st["finished"] = st["pos"] >= end
    _save(image, st)
    return st
