# 顯示格式與 HTML 報表
from datetime import datetime
from pathlib import Path

import pytest

from src import report
from src.formatting import format_duration, format_eta, format_time, human_size
from src.models import DuplicateGroup, FileInfo


@pytest.mark.parametrize("n, text", [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KB"),
                                     (5 * 1024 ** 3, "5.0 GB"), (3 * 1024 ** 4, "3.0 TB")])
def test_human_size(n, text):
    assert human_size(n) == text


@pytest.mark.parametrize("seconds, text", [(-1, "計算中…"), (3, "即將完成"), (42, "約 42 秒"),
                                           (120, "約 2 分鐘"), (200, "約 3 分 20 秒"),
                                           (3 * 3600 + 120, "約 3 小時 2 分")])
def test_format_eta(seconds, text):
    assert format_eta(seconds) == text


def test_format_duration_and_time():
    assert format_duration(12.4) == "12 秒"
    assert format_duration(80) == "1 分 20 秒"
    assert format_duration(7500) == "2 小時 5 分"
    assert format_time("2026-10-21T14:03:59") == "2026-10-21 14:03"
    assert format_time(None) == "" and format_time("not a date") == "not a date"


def _group(*names, size=100):
    return DuplicateGroup("h" + names[0], size, [FileInfo(Path(f"D:/x/{n}"), size) for n in names])


def test_ext_stats_counts_wasted_space_once_per_group():
    groups = [_group("a.jpg", "b.jpg", "c.JPG"), _group("d.png", "e.jpg", size=50),
              _group("noext", "noext2", size=10)]
    stats = dict(report.ext_stats(groups))
    assert stats[".jpg"] == {"groups": 2, "files": 4, "total_size": 350, "wasted": 200}
    assert stats[".png"]["wasted"] == 50          # 一樣多時算在先出現的副檔名
    assert stats[report.NO_EXT]["wasted"] == 10


def test_report_escapes_file_names_and_lists_roots():
    groups = [_group("<script>x.jpg", "copy.jpg")]
    html = report.build_report(groups, ["D:/Photos & more"], datetime(2026, 10, 21, 14, 3))
    assert "<script>" not in html and "&lt;script&gt;x.jpg" in html
    assert "D:/Photos &amp; more" in html and "2026-10-21 14:03" in html
    assert "<svg" in html


def test_pie_chart_handles_single_slice_and_many_slices():
    assert "<circle" in report.pie_svg([(".jpg", 10)], "t")
    many = report.pie_svg([(f".e{i}", i + 1) for i in range(30)], "t")
    assert many.count("<path") == report.MAX_SLICES and "其他" in many
    assert "沒有資料" in report.pie_svg([], "t")


def test_save_report_keeps_only_recent_reports():
    folder = report.reports_dir()
    for i in range(25):
        (folder / f"dupescan_report_20200101_0000{i:02d}.html").write_text("old", encoding="utf-8")
    path = report.save_report([_group("a.jpg", "b.jpg")], ["D:/x"])
    remaining = sorted(folder.glob("dupescan_report_*.html"))
    assert path in remaining and len(remaining) == report.KEEP_REPORTS
    for p in remaining:
        p.unlink()
