"""공유용 HTML 보고서: 텍스트 보고서(App.report)를 읽기 쉬운 한 장짜리 HTML 로 바꾼다.

내용은 텍스트 보고서와 같다(개인정보는 이미 가려져 있다). 외부 파일·스크립트 없이 한 파일로 열린다.
텍스트 보고서의 모양: 첫 줄 = 제목, "[이름] 내용" 줄 = 섹션 시작, 들여 쓴 줄 = 그 섹션의 항목.
"""
import html
import re
import time

SECTION = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
WARN_WORDS = ("실패", "[주의]", "의심 ", "멈춤", "파일 시작 표시가 있음")

CSS = """
:root { --bg: #fbfbfb; --card: #fff; --line: #e5e5e5; --text: #1b1b1b; --sub: #5f5f5f; --warn: #9d5d00; --warn-soft: #fdf3e1; --accent: #0067c0; }
@media (prefers-color-scheme: dark) { :root { --bg: #272727; --card: #2f2f2f; --line: #3b3b3b; --text: #f2f2f2; --sub: #b4b4b4; --warn: #fce100; --warn-soft: #43391a; --accent: #4cc2ff; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.6 "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 32px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.meta { color: var(--sub); font-size: 13px; margin-bottom: 24px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 18px; margin-bottom: 14px; }
h2 { font-size: 16px; margin: 0; color: var(--accent); }
.lead { margin: 4px 0 0; font-variant-numeric: tabular-nums; }
ul { margin: 8px 0 0; padding-left: 20px; } li { margin: 2px 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
li.warn, .lead.warn { color: var(--warn); }
.note { background: var(--warn-soft); }
pre { white-space: pre-wrap; font: 13px/1.5 Consolas, Menlo, monospace; margin: 8px 0 0; }
"""


def to_html(text, now=None):
    lines = text.splitlines()
    title = lines[0] if lines else "복구 보고서"
    meta, sections, cur = [], [], None
    for line in lines[1:]:
        if not line.strip():
            continue
        m = SECTION.match(line)
        if m and not line.startswith(" "):
            if m.group(1) == "형식":
                meta.append(line)
                continue
            cur = {"name": m.group(1), "lead": m.group(2), "items": []}
            sections.append(cur)
        elif cur is None:
            meta.append(line.strip())
        else:
            cur["items"].append(line.strip().lstrip("- ").strip())
    out = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>%s</title><style>%s</style></head><body><main>" % (html.escape(title), CSS),
           "<h1>%s</h1>" % html.escape(title),
           '<div class="meta">%s</div>' % html.escape(" · ".join(meta + [time.strftime("%Y-%m-%d %H:%M", now or time.localtime()) + " 저장"]))]
    warn = lambda s: any(w in s for w in WARN_WORDS)
    for sec in sections:
        cls = ' class="note"' if sec["name"] in ("주의", "알려진 한계") else ""
        out.append("<section%s><h2>%s</h2>" % (cls, html.escape(sec["name"])))
        if sec["lead"]:
            out.append('<p class="lead%s">%s</p>' % (" warn" if warn(sec["lead"]) else "", html.escape(sec["lead"])))
        if sec["items"]:
            out.append("<ul>" + "".join('<li%s>%s</li>' % (' class="warn"' if warn(i) else "", html.escape(i))
                                        for i in sec["items"]) + "</ul>")
        out.append("</section>")
    out.append("</main></body></html>")
    return "\n".join(out)
