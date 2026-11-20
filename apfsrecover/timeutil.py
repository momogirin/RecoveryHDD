"""촬영 시각 처리.

EXIF 촬영 시각은 현지 시각(시간대 정보 없음), MP4/MOV 의 mvhd 시각은 UTC 이다.
날짜별 폴더에서 같은 날로 묶이도록 UTC 값은 이 컴퓨터의 현지 시간대로 바꿔서 쓴다.
"""
from datetime import datetime, timezone


def to_local(taken, is_utc=False):
    if not taken:
        return None
    try:
        dt = datetime.fromisoformat(taken)
    except ValueError:
        return None
    if is_utc:
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


def local_date(rec):
    dt = to_local(rec.get("taken"), rec.get("taken_utc", False))
    return dt.strftime("%Y-%m-%d") if dt else None


def local_month(rec):
    dt = to_local(rec.get("taken"), rec.get("taken_utc", False))
    return dt.strftime("%Y-%m") if dt else None
