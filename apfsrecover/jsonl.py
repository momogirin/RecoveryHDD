"""JSON Lines 읽기. 도구가 갑자기 꺼지면 마지막 줄이 잘릴 수 있으므로 깨진 줄은 건너뛴다.
건너뛴 줄 수는 stats['skipped'] 에 더해 준다(주면)."""
import json


def read_jsonl(path, stats=None):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                if stats is not None:
                    stats["skipped"] = stats.get("skipped", 0) + 1
    return out
