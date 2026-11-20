"""JSON Lines 읽기. 도구가 갑자기 꺼지면 마지막 줄이 잘릴 수 있으므로 깨진 줄은 건너뛴다."""
import json


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
