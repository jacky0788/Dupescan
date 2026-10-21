# 新介面的結果畫面：保留／移除膠囊、每組至少保留一份、保護的資料夾、搜尋／類型／排序、移除後更新
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.models import DuplicateGroup, FileInfo
from src.results_io import load_results, save_results
from src.scan_options import ScanOptions
from src.selection import REASONS, AutoRules


def _g(size, *items):
    files = [FileInfo(Path(p), size, mtime=m) for p, m in items]
    return DuplicateGroup("h-" + items[0][0], size, files)


def sample_groups():
    return [
        _g(1000, ("D:/Photos/a.jpg", 100), ("D:/Downloads/a (1).jpg", 200)),
        _g(5000, ("D:/Video/v.mp4", 300), ("E:/Backup/v.mp4", 100), ("D:/Downloads/v.mp4", 400)),
        _g(200, ("D:/Docs/r.pdf", 100), ("E:/Backup/Docs/r.pdf", 100)),
    ]


@pytest.fixture
def page(qapp):
    from src.ui.results_page import ResultsInfo, ResultsPage
    p = ResultsPage()
    p.resize(1100, 700)
    groups = sample_groups()
    info = ResultsInfo(roots=["D:/", "E:/Backup"], scanned_at="2026-10-21T10:00:00",
                       options=ScanOptions(roots=["D:/", "E:/Backup"]), files_seen=50, elapsed=3.0,
                       unreadable=2, unreadable_samples=["D:/x（拒絕存取）", "D:/y（拒絕存取）"])
    rules = AutoRules(protected_dirs=["E:/Backup/Docs"])
    from src.selection import auto_select
    auto_select(groups, rules)
    p.set_results(groups, info, rules)
    p.show()
    qapp.processEvents()
    return p


def names(groups):
    return [g.files[0].name for g in groups]


def kept(g):
    return [fi.path.as_posix() for fi in g.files if not fi.selected]


def test_header_and_bottom_bar(page):
    assert page.title.text().startswith("3 組重複")
    assert "檢查了 50 個檔案" in page.meta.text()
    assert not page.unreadable_button.isHidden() and "2" in page.unreadable_button.text()
    # a (1).jpg、v.mp4 兩份副本、r.pdf 不在保護資料夾的那份
    assert page.summary.text() == "將移除 4 個檔案，釋放 10.9 KB"
    assert page.remove_button.isEnabled() and "4" in page.remove_button.text()


def test_protected_file_shows_protected_and_cannot_be_removed(page):
    model = page.model
    g = next(g for g in model.groups if g.files[0].name == "r.pdf")
    backup = g.files[1]
    assert model.state(backup) == "protected" and model.note(g, backup) == REASONS["protected"]
    messages = []
    page.message.connect(lambda text, kind: messages.append(text))
    page._toggle(backup, g)
    assert not backup.selected and "保護" in messages[-1]


def test_every_group_keeps_at_least_one(page):
    model = page.model
    g = next(g for g in model.groups if g.files[0].name == "a.jpg")
    original = g.files[0]
    messages, edits = [], []
    page.message.connect(lambda text, kind: messages.append(text))
    page.selection_edited.connect(lambda: edits.append(1))
    page._toggle(original, g)                               # 唯一保留的那份 → 拒絕
    assert not original.selected and "至少要保留一份" in messages[-1] and not edits
    page._toggle(g.files[1], g)                             # 副本改成保留
    assert kept(g) == ["D:/Photos/a.jpg", "D:/Downloads/a (1).jpg"] and g.keep_reason == "手動選擇"
    page._toggle(original, g)                               # 現在可以移除原本那份
    assert kept(g) == ["D:/Downloads/a (1).jpg"] and edits == [1, 1]
    assert page.summary.text().startswith("將移除 4 個檔案")


def test_keep_only_keep_all_and_reselect(page):
    model = page.model
    g = next(g for g in model.groups if g.files[0].name == "v.mp4")
    model.keep_only(g.files[2], g)
    assert kept(g) == ["D:/Downloads/v.mp4"]
    model.keep_all(g)
    assert len(kept(g)) == 3 and g.keep_reason == "全部保留"
    model.reselect(g)
    assert kept(g) == ["E:/Backup/v.mp4"] and g.keep_reason == REASONS["older"]


def test_search_type_and_sort(page, qapp):
    model = page.model
    assert names(model.visible_groups) == ["v.mp4", "a.jpg", "r.pdf"]        # 可釋放最多
    page.sort_combo.setCurrentIndex(page.sort_combo.findData("count"))
    assert names(model.visible_groups) == ["v.mp4", "a.jpg", "r.pdf"]
    page.sort_combo.setCurrentIndex(page.sort_combo.findData("name"))
    assert names(model.visible_groups) == ["a.jpg", "r.pdf", "v.mp4"]
    page.type_combo.setCurrentIndex(page.type_combo.findData("image"))
    assert names(model.visible_groups) == ["a.jpg"] and page.count_label.text() == "顯示 1 / 3 組"
    assert "不在目前的搜尋結果中" in page.summary_hint.text()
    page.type_combo.setCurrentIndex(0)
    page.search.setText("backup")
    page._apply_view()                                    # 不等防抖計時器
    assert sorted(names(model.visible_groups)) == ["r.pdf", "v.mp4"]
    page.search.setText("沒有這個")
    page._apply_view()
    assert not page.no_match.isHidden() and page.view.isHidden()


def test_files_removed_updates_groups(page):
    page.files_removed({str(Path("D:/Downloads/a (1).jpg")), str(Path("D:/Downloads/v.mp4"))})
    assert sorted(names(page.model.groups)) == ["r.pdf", "v.mp4"]      # a.jpg 只剩一份，不再是重複
    g = next(g for g in page.model.groups if g.files[0].name == "v.mp4")
    assert len(g.files) == 2
    assert page.title.text().startswith("2 組重複")
    page.files_removed({str(fi.path) for g in page.model.groups for fi in g.files[1:]})
    assert page.body.currentIndex() == 1 and page.bottom.isHidden()


def test_apply_rules_replaces_manual_choices(page):
    g = next(g for g in page.model.groups if g.files[0].name == "v.mp4")
    page.model.keep_all(g)
    counts = page.apply_rules(AutoRules(fallback="newest"))
    assert kept(g) == ["D:/Downloads/v.mp4"] and sum(counts.values()) == 3
    r = next(g for g in page.model.groups if g.files[0].name == "r.pdf")
    assert page.model.state(r.files[1]) == "remove"         # 不再有保護的資料夾


def _click(view, index, column_offset=None):
    rect = view.visualRect(index)
    pos = QPoint(rect.left() + (column_offset if column_offset is not None else rect.width() // 2),
                 rect.center().y())
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def test_view_click_on_pill_toggles_and_group_click_collapses(page, qapp):
    view, model = page.view, page.model
    top = model.index(1, 0)                                 # a.jpg
    g = model.group_at(top)
    copy_index = model.index(1, 0, top)                     # a (1).jpg（移除）
    assert model.file_at(copy_index).selected
    _click(view, copy_index, column_offset=view.indentation() * 2 + 20)
    assert not model.file_at(copy_index).selected and g.keep_reason == "手動選擇"
    view.setCurrentIndex(copy_index)
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert model.file_at(copy_index).selected
    assert view.isExpanded(top)
    _click(view, top)
    assert not view.isExpanded(top)
    page._apply_view()                                      # 重新整理後維持收合
    assert not view.isExpanded(model.index(model.visible_groups.index(g), 0))


def test_results_info_survives_save_and_load(page, tmp_path):
    from src.ui.results_page import ResultsInfo
    path = tmp_path / "r.dupescan"
    info = page.info
    save_results(path, page.groups(), info.roots, info.scanned_at, scan=info.to_scan_dict())
    groups, meta = load_results(path)
    loaded = ResultsInfo.from_meta(meta, str(path))
    assert loaded.files_seen == 50 and loaded.unreadable == 2 and loaded.options == info.options
    assert loaded.rescan_options() == info.options
    assert [g.keep_reason for g in groups] == [g.keep_reason for g in page.groups()]
    assert ResultsInfo.from_meta({"roots": ["D:/a"], "scan": {"options": {"roots": []}}}).rescan_options() \
        == ScanOptions(roots=["D:/a"])
