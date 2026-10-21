# HTML 分析報表：依副檔名統計重複檔案與可釋放空間，附兩張圓餅圖與最佔空間的群組。
# 純邏輯、不依賴 Qt；報表存到 %LOCALAPPDATA%\DupeScan\reports，只保留最近 20 份。
import math
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path

from .app_paths import data_dir
from .formatting import human_size
from .models import DuplicateGroup

KEEP_REPORTS = 20
MAX_SLICES   = 10      # 圓餅圖最多幾塊，其餘合併成「其他」
TOP_GROUPS   = 20      # 「最佔空間的重複」列出幾組
NO_EXT       = "（無副檔名）"

_COLORS = ["#5b8def", "#46c08a", "#e3b341", "#e5534b", "#b083f0",
           "#39c5cf", "#f778ba", "#8ddb8c", "#f0883e", "#8b949e"]


def _ext(path: Path) -> str:
    return path.suffix.lower() or NO_EXT


def ext_stats(groups: list[DuplicateGroup]) -> list[tuple[str, dict]]:
    """每個副檔名的 {groups, files, total_size, wasted}，依可釋放空間由大到小。

    一組裡副檔名不一樣時（內容相同、只是改了副檔名），可釋放空間算在數量最多的那個副檔名。
    """
    data: dict[str, dict] = defaultdict(lambda: {"groups": set(), "files": 0,
                                                 "total_size": 0, "wasted": 0})
    for index, g in enumerate(groups):
        counts: dict[str, int] = defaultdict(int)
        for fi in g.files:
            ext = _ext(fi.path)
            counts[ext] += 1
            data[ext]["groups"].add(index)
            data[ext]["files"] += 1
            data[ext]["total_size"] += fi.size
        data[max(counts, key=counts.__getitem__)]["wasted"] += g.wasted_bytes
    rows = [(ext, {**d, "groups": len(d["groups"])}) for ext, d in data.items()]
    rows.sort(key=lambda r: (-r[1]["wasted"], -r[1]["files"], r[0]))
    return rows


def _slices(pairs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    pairs = sorted((p for p in pairs if p[1] > 0), key=lambda p: -p[1])
    if len(pairs) > MAX_SLICES:
        rest = sum(v for _, v in pairs[MAX_SLICES - 1:])
        pairs = pairs[:MAX_SLICES - 1] + [("其他", rest)]
    return pairs


def pie_svg(pairs: list[tuple[str, int]], title: str) -> str:
    """[(標籤, 數值)] → 帶圖例的 SVG 圓餅圖。"""
    slices = _slices(pairs)
    total = sum(v for _, v in slices)
    if total == 0:
        return "<p class='muted'>沒有資料</p>"
    width, height, cx, cy, r = 500, 300, 150, 160, 120
    out = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape(title)}">',
           f'<text x="0" y="16" font-size="13" fill="#e7e9ee" font-weight="600">{escape(title)}</text>']
    start = -math.pi / 2
    for i, (label, value) in enumerate(slices):
        color = _COLORS[i % len(_COLORS)]
        share = value / total
        if share >= 0.9999:                       # 只有一塊：畫整個圓（弧線畫不出 360 度）
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        else:
            end = start + 2 * math.pi * share
            x1, y1 = cx + r * math.cos(start), cy + r * math.sin(start)
            x2, y2 = cx + r * math.cos(end), cy + r * math.sin(end)
            large = 1 if share > 0.5 else 0
            out.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large},1 '
                       f'{x2:.1f},{y2:.1f} Z" fill="{color}" stroke="#121418" stroke-width="1.5"/>')
            start = end
        y = 46 + i * 24
        short = label if len(label) <= 14 else label[:13] + "…"
        out.append(f'<rect x="300" y="{y - 11}" width="12" height="12" rx="3" fill="{color}"/>')
        out.append(f'<text x="320" y="{y}" font-size="12" fill="#e7e9ee">{escape(short)}'
                   f'<tspan fill="#a3a9b5">　{share * 100:.1f}%</tspan></text>')
    out.append("</svg>")
    return "\n".join(out)


_CSS = """
*{box-sizing:border-box}
body{font-family:"Segoe UI","Microsoft JhengHei UI",sans-serif;background:#121418;color:#e7e9ee;
     margin:0;padding:32px;line-height:1.5}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 10px;color:#a3a9b5;font-weight:600}
.muted{color:#a3a9b5}
.meta{color:#a3a9b5;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:20px}
.card{background:#1a1d23;border:1px solid #2c313a;border-radius:12px;padding:16px}
.card .v{font-size:22px;font-weight:700}
.card .l{font-size:12px;color:#a3a9b5}
.charts{display:flex;flex-wrap:wrap;gap:12px}
.box{background:#1a1d23;border:1px solid #2c313a;border-radius:12px;padding:16px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#a3a9b5;font-weight:600;padding:8px 10px;border-bottom:1px solid #2c313a}
td{padding:8px 10px;border-bottom:1px solid #22262e;vertical-align:top}
td.n{text-align:right;white-space:nowrap}
.bar{background:#22262e;border-radius:4px;height:8px;width:120px;display:inline-block;vertical-align:middle}
.fill{background:#5b8def;border-radius:4px;height:8px}
.path{color:#a3a9b5;font-size:12px;word-break:break-all}
footer{color:#6d7380;font-size:12px;margin-top:32px}
"""


def build_report(groups: list[DuplicateGroup], roots: list[str],
                 generated_at: datetime | None = None) -> str:
    """產生完整的 HTML 報表內容。"""
    generated_at = generated_at or datetime.now()
    rows = ext_stats(groups)
    total_files = sum(len(g.files) for g in groups)
    total_size = sum(g.size * len(g.files) for g in groups)
    total_wasted = sum(g.wasted_bytes for g in groups)

    cards = "".join(
        f'<div class="card"><div class="v">{escape(value)}</div><div class="l">{label}</div></div>'
        for value, label in [(f"{len(groups):,}", "重複的組數"), (f"{total_files:,}", "重複的檔案"),
                             (human_size(total_wasted), "最多可釋放"),
                             (human_size(total_size), "重複檔案合計")])
    charts = (f'<div class="box">{pie_svg([(e, d["wasted"]) for e, d in rows], "可釋放空間（依副檔名）")}</div>'
              f'<div class="box">{pie_svg([(e, d["files"]) for e, d in rows], "重複檔案數（依副檔名）")}</div>')

    ext_rows = []
    for ext, d in rows:
        share = d["wasted"] / total_wasted * 100 if total_wasted else 0
        ext_rows.append(
            f"<tr><td>{escape(ext)}</td><td class='n'>{d['groups']:,}</td><td class='n'>{d['files']:,}</td>"
            f"<td class='n'>{human_size(d['total_size'])}</td><td class='n'>{human_size(d['wasted'])}</td>"
            f"<td><span class='bar'><div class='fill' style='width:{min(share, 100):.1f}%'></div></span>"
            f" {share:.1f}%</td></tr>")

    top_rows = []
    for g in sorted(groups, key=lambda g: -g.wasted_bytes)[:TOP_GROUPS]:
        paths = "<br>".join(escape(str(fi.path)) for fi in g.files[:6])
        if len(g.files) > 6:
            paths += f"<br>… 另外 {len(g.files) - 6} 份"
        top_rows.append(
            f"<tr><td>{escape(g.files[0].name)}<div class='path'>{paths}</div></td>"
            f"<td class='n'>{len(g.files)}</td><td class='n'>{human_size(g.size)}</td>"
            f"<td class='n'>{human_size(g.wasted_bytes)}</td></tr>")

    where = "；".join(escape(r) for r in roots) or "（未記錄）"
    return f"""<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="utf-8"><title>DupeScan 報表</title>
<style>{_CSS}</style></head><body>
<h1>DupeScan 報表</h1>
<div class="meta">掃描的資料夾：{where}<br>產生時間：{generated_at:%Y-%m-%d %H:%M}</div>
<div class="cards">{cards}</div>
<h2>分布</h2>
<div class="charts">{charts}</div>
<h2>依副檔名</h2>
<div class="box"><table><thead><tr><th>副檔名</th><th>組數</th><th>檔案數</th><th>合計大小</th>
<th>可釋放</th><th>占可釋放空間</th></tr></thead><tbody>{''.join(ext_rows)}</tbody></table></div>
<h2>最佔空間的重複（前 {TOP_GROUPS} 組）</h2>
<div class="box"><table><thead><tr><th>檔案</th><th>份數</th><th>每份</th><th>可釋放</th></tr></thead>
<tbody>{''.join(top_rows)}</tbody></table></div>
<footer>由 DupeScan 產生。報表只是統計，不會更動任何檔案。</footer>
</body></html>"""


def reports_dir() -> Path:
    path = data_dir() / "reports"
    path.mkdir(exist_ok=True)
    return path


def save_report(groups: list[DuplicateGroup], roots: list[str]) -> Path:
    """寫入報表檔並回傳路徑；舊報表只留最近 KEEP_REPORTS 份（都是 DupeScan 自己產生的檔案）。"""
    now = datetime.now()
    folder = reports_dir()
    path = folder / f"dupescan_report_{now:%Y%m%d_%H%M%S}.html"
    path.write_text(build_report(groups, roots, now), encoding="utf-8")
    for old in sorted(folder.glob("dupescan_report_*.html"))[:-KEEP_REPORTS]:
        try:
            old.unlink()
        except OSError:
            pass
    return path
