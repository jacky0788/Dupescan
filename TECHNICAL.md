# TECHNICAL.md — DupeScan 技術參考手冊

> 供技術人員維護、擴充、除錯時參考。對應版本：v0.9.0。

---

## 目錄

- [技術堆疊](#技術堆疊)
- [專案架構](#專案架構)
- [模組說明](#模組說明)
- [掃描演算法](#掃描演算法)
- [雜湊快取](#雜湊快取)
- [Threading 模型](#threading-模型)
- [狀態機設計](#狀態機設計)
- [移除流程](#移除流程)
- [UI 虛擬化結果檢視](#ui-虛擬化結果檢視)
- [設定與資料位置](#設定與資料位置)
- [日誌系統](#日誌系統)
- [測試](#測試)
- [效能考量](#效能考量)
- [關鍵常數](#關鍵常數)
- [歷史問題與修正](#歷史問題與修正)

---

## 技術堆疊

| 技術 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+（開發環境 3.14） | 主要語言 |
| PyQt6 | 6.6+ | GUI 框架 |
| xxhash | 3.4+ | 高速 Hash（備援：hashlib.blake2b）|
| humanize | 4.9+ | 人性化顯示檔案大小 |
| sqlite3 | 內建 | 雜湊快取 |
| ctypes（Win32 API） | 內建 | 磁碟偵測、資源回收筒、磁碟區資訊 |
| pytest | 8+（開發用） | 自動化測試 |

---

## 專案架構

```
dupescan/
├── main.py                  # 入口：setup_logging → QApplication → MainWindow
├── run.bat                  # 一鍵啟動
├── requirements.txt / requirements-dev.txt / pytest.ini
├── src/
│   ├── __init__.py          # __version__
│   ├── models.py            # FileInfo、DuplicateGroup
│   ├── scanner.py           # 掃描引擎（不依賴 Qt）
│   ├── hash_cache.py        # SQLite 雜湊快取
│   ├── disk_detect.py       # 磁碟類型偵測（ctypes）
│   ├── selection.py         # 快速選取條件、智慧規則、參考資料夾、預設集（不依賴 Qt）
│   ├── delete_plan.py       # 移除前檢查（不依賴 Qt）
│   ├── file_ops.py          # 資源回收筒／隔離資料夾／還原／操作紀錄（不依賴 Qt）
│   ├── results_io.py        # 結果 CSV、儲存與開啟（不依賴 Qt）
│   ├── app_paths.py         # %LOCALAPPDATA%\DupeScan
│   ├── logger.py            # 日誌
│   └── ui/
│       ├── main_window.py   # ScanWorker、ImagePreviewPopup、DuplicateTree、MainWindow、ConditionPanel
│       ├── delete_dialog.py # DeletePlanDialog、RemovalWorker
│       ├── history_dialog.py# HistoryDialog
│       └── theme.py         # 配色常數與全域 StyleSheet
└── tests/                   # pytest
```

原則：**掃描、選取、移除檢查、檔案動作、結果存檔都不依賴 Qt**，UI 只負責呼叫與顯示。這些模組可以直接被其他專案（例如音樂管理專案）import。

---

## 模組說明

### `src/models.py`

#### `FileInfo`

| 欄位 | 型別 | 說明 |
|------|------|------|
| `path` | `Path` | 檔案絕對路徑 |
| `size` | `int` | 檔案大小（bytes）|
| `hash_full` | `str` | 完整 Hash（Pass 3 後填入）|
| `mtime` / `mtime_ns` | `float` / `int` | 修改時間（秒／奈秒；奈秒用於快取與移除前複驗）|
| `selected` | `bool` | 是否勾選待移除（存在資料層，與 UI 解耦）|
| `dev` / `ino` | `int` | 磁碟區序號與檔案 ID（`st_dev`／`st_ino`），只對候選檔查詢；`ino=0` 表示檔案系統不提供 |
| `aliases` | `list[str]` | 指向同一個實體檔的其他路徑（硬連結），不列為重複 |
| `.name` / `.folder` | property | 檔名／所在目錄字串 |

#### `DuplicateGroup`

| 欄位 | 說明 |
|------|------|
| `hash_value` | 群組共同的完整 Hash |
| `size` | 單一檔案大小 |
| `files` | 群組內所有副本（依路徑排序）|
| `.wasted_bytes` | `size × (len(files) - 1)` |

---

### `src/scanner.py`

#### `Scanner`

```python
Scanner(roots, min_size=1, include_hidden=False, pass2_workers=1, pass3_workers=1,
        on_progress=None, on_done=None, on_error=None, on_cancel=None,
        cache=None, max_size=None, include_exts=(), exclude_exts=(), exclude_dirs=(),
        workers_by_dev=None)
```

| 參數 | 說明 |
|------|------|
| `roots` | 掃描路徑；經 `normalize_roots()` 轉成絕對路徑並去掉重複或互相包含的路徑 |
| `min_size` / `max_size` | 大小範圍（bytes）；`max_size=None` 表示不限 |
| `include_hidden` | 是否包含「.」開頭與 Windows 隱藏／系統屬性的檔案和資料夾 |
| `include_exts` / `exclude_exts` | 只比對／不比對的副檔名（不含點、不分大小寫）|
| `exclude_dirs` | 排除資料夾：相對名稱（任何位置）或絕對路徑 |
| `pass2_workers` / `pass3_workers` | 預設執行緒數 |
| `workers_by_dev` | `{st_dev: (pass2, pass3)}`，多顆磁碟時各用各的執行緒數 |
| `cache` | `HashCache`；`None` 表示不使用快取 |
| `on_progress(msg, cur, total, step, total_steps, eta)` | 進度；`eta=-1` 表示無法估算 |
| `on_done(groups, stats)` | 完成 |
| `on_cancel()` | 被停止（**不會**呼叫 `on_done`）|
| `on_error(msg)` | 例外 |

| 方法 | 說明 |
|------|------|
| `scan()` | 執行掃描；完成回傳 groups，停止或錯誤回傳 `None` |
| `stop()` / `pause()` / `resume()` | 可從任意執行緒呼叫，也可在 `scan()` 開始前呼叫 |

#### `ScanStats`

`files_seen`、`candidates`、`same_file_merged`、`skipped_links`、`skipped_hidden`、`skipped_cloud`、`skipped_excluded`、`skipped_changing`、`unreadable`（含最多 200 筆 `unreadable_samples`）、`cache_hits_partial`、`cache_hits_full`。

#### 內部結構

| 方法 | 說明 |
|------|------|
| `_checkpoint(nbytes=0)` | 暫停點：暫停時阻塞、停止時拋出 `_Stopped`；累加已讀位元組供 Pass 3 估算進度 |
| `_walk()` | Pass 1 遍歷：`os.scandir` 堆疊式走訪；略過符號連結與 junction／掛載點、排除資料夾、隱藏／系統、雲端佔位檔；檔案資訊用 `DirEntry.stat()`（Windows 上不必開檔）|
| `_merge_same_files(candidates)` | 對大小相同的候選 `os.stat` 取得 `st_dev`／`st_ino`，合併同一個實體檔；目錄列舉後大小又變的檔案略過 |
| `_hash_stage(files, fn, step, label)` | Pass 2／3：依 `fi.dev` 分批，每顆磁碟用自己的執行緒數 |
| `_hash_batch(files, fn, workers)` | 單一磁碟：循序或 `ThreadPoolExecutor`（在途工作上限 `workers × IN_FLIGHT_PER_WORKER`）|
| `_report(force=False)` | 時間節流的進度；Pass 3 以位元組估算 ETA |
| `_run()` / `_run_passes()` | 開關快取、執行 3-pass；`_Stopped` → 回傳 `None` |

讀檔函式 `_partial_hash(path, checkpoint)`、`_full_hash(path, checkpoint)`：讀取失敗拋 `OSError`（記入 `unreadable`）；`_full_hash` 每讀一個 `CHUNK_SIZE` 區塊呼叫一次 checkpoint。

---

### `src/hash_cache.py`

見[雜湊快取](#雜湊快取)。

| 名稱 | 說明 |
|------|------|
| `HashCache(path=None)` | `open()` → `lookup(files, algo, partial_len)` → `store(fi, algo, partial=…, full=…)` → `close()`；全部在掃描執行緒呼叫 |
| `cache_key(fi)` | `i:<dev>:<ino>`（本機 NTFS／ReFS）或 `p:<normcase(path)>` |
| `clear_cache()` | 刪除快取檔（DupeScan 自己的檔案）|

---

### `src/disk_detect.py`

掃描開始前由 `ScanWorker.run()` 對每個掃描路徑呼叫一次，結果決定該磁碟的執行緒數。

| 磁碟類型 | Pass 2 | Pass 3 |
|---------|--------|--------|
| NVMe SSD | 8 | 4 |
| SATA SSD／其他 SSD | 4 | 2 |
| HDD | 1 | 1 |
| 未知／網路 | 2 | 1 |

偵測方式：`CreateFileW`（access=0，不需系統管理員）→ `IOCTL_STORAGE_QUERY_PROPERTY`：PropertyId=7（SeekPenalty）區分 HDD／SSD，PropertyId=0（BusType：NVMe=17、SATA=11）區分 NVMe／SATA。任何例外都回傳保守的「未知磁碟」。

---

### `src/selection.py`

決定每個群組保留哪一份、勾選哪些。

| 名稱 | 說明 |
|------|------|
| `CONDITIONS_DEF` | 12 條：9 條基本條件（id、標籤、key 函式、reverse、互斥 id）＋ 3 條智慧規則 `copy_name`／`folders`／`custom`（key 與 reverse 為 `None`）|
| `Condition(id, keys, reverse, smart)` | `keys(files)` 依整個群組算出每個檔案的排序值 |
| `condition(id, rules=None)` | 建立 `Condition`；智慧規則需要的設定從 `RuleSet` 取 |
| `rank_by_conditions(files, conditions, mode="keep", candidates=None)` | 勝出（成為目標）的檔案索引；`candidates` 限定候選 |
| `apply_selection(group, conditions, mode, reference=None)` | 更新 `selected`（`keep`＝勝出者不勾選；`delete`＝只勾選勝出者）；回傳決定勝負的條件 id，`"reference"`＝參考資料夾決定，`None`＝全部平手取列表第一 |
| `RuleSet` | 一組規則：`mode`、`conditions`（id，依優先序）、`keep_dirs`、`remove_dirs`、`reference_dirs`、`custom`（自訂規則原文）。`build()`、`reference()`、`custom_rules()`、`to_dict()`／`from_dict()` |
| `dump_presets(presets)`／`load_presets(text)` | `dict[str, RuleSet]` ↔ JSON 字串 `{"version": 1, "presets": [{"name": …, …}]}`；內容壞掉時略過壞掉的部分，不拋例外 |

**比較方式**（`_rank`）：依優先序逐一比較，每個條件只留下最好的那幾個候選，平手才交給下一個條件；全部平手時取候選中的第一個（群組內依路徑排序，結果固定）。等同以倒序套用條件的多鍵穩定排序。

- 基本條件：`reverse=True` 表示值越大越優先成為目標（保留模式＝保留它，刪除模式＝勾選它）
- 智慧規則（`smart=True`）：排序值是「該移除的程度」，**方向固定**——保留模式取最小值，刪除模式取最大值。所以「優先移除」在兩種模式下都比較容易被勾選

**智慧規則的排序值**

| id | 函式 | 說明 |
|----|------|------|
| `copy_name` | `copy_scores(files)` | `copy_info(name)` 回傳（去掉複本標記後的檔名, 標記數），檔名先 NFC 正規化、比對時 casefold。同名（去掉標記後）的檔案中，比最少的多幾個標記就是幾分；群組裡沒有同名的其他檔案時為 0 |
| `folders` | `folder_scores(files, keep, remove)` | 在優先移除的資料夾為正、優先保留的為負、都不在為 0，清單越前面絕對值越大。兩邊都符合時以比對到比較深的那邊為準，一樣深算優先保留 |
| `custom` | `custom_scores(files, compiled)` | 由上往下第一條符合的規則決定：`-` 為正、`+` 為負，越上面絕對值越大；都不符合為 0 |

**複本標記**：`_COPY_SUFFIX` 必須接在任何一個 `.` 之前或檔名結尾——` (1)`～` (99)`（含沒有空白、全形括號；`(0)`、`(100)`、`(2019)` 不算）、` - 複製／复制／副本／コピー／Copy`（可再接 ` (2)`）、macOS ` copy`／` copy 2`、Linux ` (copy)`、`.dup`／`.dup1`；`_COPY_PREFIX`：`Copy of `、`Copy (2) of `。檔名含 `+` 或 `%` 時先 `unquote_plus`，有變化就算 1 個標記。標記一次去掉一個、重複到沒有為止。檔名完全不含 `( （ - + %`、`copy`、`.dup` 時直接略過（`_COPY_HINT`）。

**資料夾清單**（`FolderList(dirs)`，輸入用 `split_dirs(text)` 以 `;` 拆開）：不分大小寫、`\` 換成 `/`，只比對檔案所在的資料夾（`_folder_of(path)` → `d:/a/b/`，結尾有 `/`）。

- 完整路徑（`x:` 開頭或 UNC `//` 開頭）：資料夾本身與其下所有子資料夾（`folder.startswith(樣式 + "/")`）
- 其他視為名稱片段：資料夾路徑中任何位置含有就算（取最後一次出現）；`/old/` 這種前後有分隔符號的，等於要整個資料夾名稱相同
- `match_folder(folder)` 回傳（比對結尾落在第幾層, 樣式在清單中的位置）；好幾個樣式符合時取最深的，一樣深取前面的。參考資料夾、`delete_plan` 也用同一套比對

**參考資料夾**（`apply_selection` 的 `reference`）：裡面的檔案一律 `selected=False`。保留模式下群組裡有參考檔案時，候選＝參考檔案（保留的那份從中選，其他副本照常勾選）；刪除模式下候選＝非參考檔案（整組都是參考檔案時不勾選）。

**自訂規則**：`parse_custom_rules(text)` → (`list[CustomRule]`, 錯誤訊息)。每行 `+ 樣式`（優先保留）或 `- 樣式`（優先移除），也接受全形 `＋`／`－`；`+`／`-` 後面的空白忽略；空行與 `#` 開頭的行略過；以 `re.IGNORECASE` 編譯，失敗的行列入錯誤訊息。比對時對原本的路徑（`D:\a\b.png`）與 `/` 分隔的路徑（`D:/a/b.png`）各試一次，任一符合就算。

---

### `src/delete_plan.py`

見[移除流程](#移除流程)。

| 名稱 | 說明 |
|------|------|
| `build_plan(groups, protected=None, verify_head=True, reference_dirs=None)` | `groups` 為 `(顯示編號, DuplicateGroup)`；`reference_dirs` 為參考資料夾（寫法同 `selection.FolderList`），裡面被勾選的檔案視同受保護；回傳 `DeletePlan` |
| `DeletePlan.removable(allow_whole_group, allow_protected)` | 實際要移除的 `(PlanGroup, PlanEntry)`；`allow_protected` 同時放行受保護與參考資料夾 |
| `PlanEntry.status` | `keep`／`ok`（會移除）／`skip`（自動略過）／`protected`（`note` 為 `PROTECTED_NOTE`「位於受保護資料夾」或 `REFERENCE_NOTE`「位於參考資料夾」）|
| `DEFAULT_PROTECTED` | 受保護資料夾的 fnmatch 樣式 |
| `is_protected(path)` | 是否位於受保護資料夾 |

判斷順序：掃描後有變動（`skip`）→ 受保護資料夾 → 參考資料夾 → `ok`。

---

### `src/file_ops.py`

| 名稱 | 說明 |
|------|------|
| `remove_files(items, method, hwnd, progress)` | `items` 為 `(Path, size)`；`method="recycle"` 或 `"quarantine"`；回傳 `list[OpResult]` 並寫入操作紀錄 |
| `recycle_obstacle(path, size)` | 進不了回收筒的原因（路徑 > 259 字元、網路／卸除式磁碟、回收筒停用 `NukeOnDelete`、超過 `MaxCapacity`）|
| `quarantine(paths, batch_id)` | 搬到 `<磁碟根目錄>\.dupescan_quarantine\<batch>\<原路徑>` 並寫 `manifest.json` |
| `restore_batch(manifest)` | 搬回原位置；原位置已有檔案時不覆蓋 |
| `read_journal()` | 讀 `journal.jsonl`（新的在前）|

資源回收筒使用 `SHFileOperationW(FO_DELETE)`，旗標 `FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT | FOF_WANTNUKEWARNING`：若 Windows 判斷某個檔案只能永久刪除，會先跳出警告讓使用者拒絕。每 50 個檔案一批；整批失敗時逐一重試，仍失敗的改走隔離資料夾。

---

### `src/results_io.py`

`export_csv(path, groups)`（UTF-8 BOM，Excel 可直接開）、`save_results(path, groups, roots, scanned_at)`（gzip JSON，`format="dupescan-results"`、`version=1`）、`load_results(path)`（格式不符拋 `ValueError`）。

---

### `src/logger.py`、`src/app_paths.py`

- `setup_logging()`：由 `main.py` 呼叫才建立 log 檔（單純 import 不建立）；保留最近 `KEEP_LOGS` 份
- `data_dir()`：`%LOCALAPPDATA%\DupeScan`，可用環境變數 `DUPESCAN_DATA_DIR` 改位置

---

### `src/ui/main_window.py`

#### `ScanWorker(QObject)`

`moveToThread` 到 `QThread` 執行。建構時就建立 `Scanner`，所以 `run()` 開始前呼叫 `stop()` 也有效。

| Signal | 參數 | 說明 |
|--------|------|------|
| `progress` | `(scan_id, msg, cur, total, step, total_steps, eta)` | 進度 |
| `finished` | `(scan_id, list, ScanStats)` | 完成 |
| `cancelled` | `(scan_id)` | 被停止 |
| `error` | `(scan_id, str)` | 例外 |
| `detected` | `(scan_id, str)` | 磁碟偵測結果（多顆磁碟時合併顯示）|
| `ended` | `()` | `run()` 結束，接到 `thread.quit` |

#### `DuplicateTree(QTreeWidget)`

頂層列＝群組、子列＝檔案（展開時才建立）；勾選狀態存在 `FileInfo.selected`。字型由 `_FontDelegate` 依 `_font_size` 繪製，改字級只需重新排版。重點方法：`populate(groups, expanded=None)`、`expanded_group_ids()`、`refresh_checks()`、`select_all(sel, reference=None)`（勾選時略過參考資料夾）、`apply_conditions_all(conditions, mode, reference=None)`（回傳各條件決定了幾組的 `Counter`）、`selected_stats()`、`set_font_size()`、`set_reference()`（參考資料夾的檔名標 📌）、`keyPressEvent()`（空白鍵切換勾選）、`_on_clicked()`（Ctrl＋點擊路徑定位）、`_context_menu(item)`（建立右鍵選單，測試直接檢查）。

右鍵選單要用的目前規則與預設集由主視窗指定：`rules_provider()` → `RuleSet`、`presets_provider()` → `dict[str, RuleSet]`。手動操作（點勾選框、空白鍵、「勾選此群組全部」）不受參考資料夾限制，由移除前的檢查把關。

#### `ConditionPanel(QWidget)`

側邊列的條件面板：基本條件與智慧規則的勾選框（`_checkboxes`，勾選順序存在 `_checked_order`＝優先序，互斥的自動停用）、勾選「資料夾優先序」「自訂規則」才顯示的設定欄位（`keep_dirs_edit`、`remove_dirs_edit`、`custom_edit`，自訂規則一邊打一邊檢查）、參考資料夾 `reference_edit`（停手 0.3 秒發出 `referenceChanged`）、「優先序」說明。`rule_set()` 讀出 `RuleSet`（`mode` 由主視窗填），`set_rule_set(rules)` 載入預設集。

#### `MainWindow(QMainWindow)`

| 欄位 | 說明 |
|------|------|
| `_scan_id` | 掃描流水號，用來忽略舊掃描的遲到訊號 |
| `_threads` | `{scan_id: (QThread, ScanWorker)}`，等 `finished` 送達才釋放 |
| `_thread` / `_worker` | 目前這次掃描 |
| `_removal` | 背景移除中的 `(QThread, RemovalWorker, QProgressDialog)` |
| `_all_groups` / `_groups` | 完整結果／篩選排序後顯示的結果 |
| `_settings` | `QSettings`（INI）|
| `_presets` | 具名預設集 `dict[str, RuleSet]` |

| 方法 | 說明 |
|------|------|
| `_start_scan()` | 驗證路徑 → `_stop_worker()` → 建立 worker／thread → 開始 |
| `_stop_worker(wait_ms)` | 要求停止並等待；等不到也不釋放參照 |
| `_on_thread_finished(scan_id)` | 該執行緒 `finished` 送達：此時才釋放 |
| `_on_done` / `_on_cancelled` / `_on_error` | 只處理 `scan_id` 相符的訊號 |
| `_current_rules()` | 側邊列目前的 `RuleSet`（操作模式＋條件面板）|
| `_apply_rules_to_all(rules)` | 套用到所有顯示中的群組，回傳狀態列訊息；沒有條件或自訂規則有誤時提示並擋下 |
| `_apply_preset()` / `_save_preset()` / `_delete_preset()` | 預設集：載入並套用／另存（同名先確認）／刪除（先確認）；`_store_presets()` 立即寫進設定檔 |
| `_select_all()` | 「全部勾選」，略過參考資料夾 |
| `_delete_selected()` | `build_plan(..., reference_dirs=…)` → `DeletePlanDialog` → `_run_removal()` |
| `_on_removal_done(results)` | `_apply_removal()`（就地更新結果，不重掃）→ `_report_removal()` |
| `_load_settings()` / `_save_settings()` | 啟動時還原、關閉時記住 |
| `_export_csv()` / `_save_results()` / `_open_results()` | 結果匯出與存檔 |
| `_export_report()` | HTML 報表（存到 `%LOCALAPPDATA%\DupeScan\reports`，保留 20 份）|
| `closeEvent()` | 記住設定、停止掃描、等待背景執行緒 |

---

### `src/ui/delete_dialog.py`、`history_dialog.py`、`theme.py`

- `DeletePlanDialog(plan)`：預覽與確認；呼叫端取 `selected()`、`method()`、`full_verify()`
- `RemovalWorker(items, method, hwnd, full_verify)`：背景執行；`full_verify` 時先重算完整 Hash，與掃描結果不符的略過
- `HistoryDialog`：列出 `journal.jsonl`，可還原隔離的檔案
- `theme.py`：配色常數與 `STYLE`。注意 `QTreeWidget::item` 不設定 `color`，否則會蓋掉各列的 `ForegroundRole`

---

## 掃描演算法

```
所有檔案 (N 個)
    │
    ▼ Pass 1：os.scandir 遍歷，DirEntry.stat() 取大小（不開檔），依 size 分組
    │  ── 刪除 size 唯一的 key
    │  ── 候選 os.stat 取 (st_dev, st_ino)，合併同一實體檔（硬連結）
    │
    ▼ 快取查詢：大小、修改時間（ns）、演算法都相同 → 直接沿用 partial／full
    │
    ▼ Pass 2：讀前 PARTIAL_READ (4KB) 算 hash，以 (size, partial) 分組
    │  ── 刪除只有一個成員的組
    │
    ▼ Pass 3：讀整個檔案算 hash，以 (size, full) 分組
    │
    └─→ list[DuplicateGroup]：群組內依路徑排序，群組依 wasted_bytes 由大到小
```

| Pass | 磁碟 I/O | 說明 |
|------|---------|------|
| Pass 1 | 只列目錄；候選另做一次 stat | 每檔約 4 µs（暖快取）|
| Pass 2 | 每個未命中快取的候選讀 4 KB | HDD 上以尋軌時間為主 |
| Pass 3 | 未命中快取的候選完整讀取 | 真正的重複檔一定要完整讀過一次；第二次起由快取省掉 |

---

## 雜湊快取

- 位置：`%LOCALAPPDATA%\DupeScan\hashcache.sqlite3`（WAL 模式）
- 資料表 `files(key, size, mtime_ns, algo, partial, partial_len, full, seen)`
- **鍵**：本機 NTFS／ReFS（`GetDriveTypeW` 為 FIXED／REMOVABLE 且 `GetVolumeInformationW` 的檔案系統為 NTFS／ReFS）用 `i:<dev>:<ino>`，改名、搬移仍認得；其他（FAT／exFAT、網路磁碟）的檔案 ID 可能被重複使用，用 `p:<路徑>`
- **有效條件**：`size`、`mtime_ns`、`algo` 都相同；`partial` 另需 `partial_len == PARTIAL_READ`
- **寫入**：在掃描執行緒累積，每 `FLUSH_EVERY` 筆或掃描結束時寫入；**取消時也會寫入**
- **清理**：`seen` 超過 `PRUNE_AFTER_DAYS`（365 天）的紀錄在掃描結束時刪除
- 風險：內容改了但大小與修改時間都沒變（極少見）→ 由移除前的開頭 4 KB 複驗與可選的完整比對把關

---

## Threading 模型

```
Main Thread（Qt 事件迴圈）
  ├─ QThread「掃描」── ScanWorker.run() ── Scanner.scan()
  │                                          └─ ThreadPoolExecutor（Pass 2／3，SSD／NVMe）
  └─ QThread「移除」── RemovalWorker.run() ── file_ops.remove_files()

Worker → Main：Qt signals（第一個參數是 scan_id）
Main → Worker：Scanner 的 threading.Event（stop／pause）
```

- **暫停／停止**：`_checkpoint()` 在每個讀檔區塊之間呼叫（worker 執行緒也一樣），所以暫停會真的停住 I/O，停止不必等大檔讀完
- **`stop()` 必須同時 set pause_event**，否則暫停中的執行緒收不到停止訊號
- **執行緒生命週期**：`worker.ended → thread.quit`；`(thread, worker)` 存在 `_threads[scan_id]`，直到該執行緒的 `finished` 送達 `_on_thread_finished(scan_id)` 才釋放。執行中的 QThread 被回收，或 `finished` 還在排隊時就被回收，Qt 都會直接終止程式
- **關閉視窗**：先停止掃描並等待；仍未結束的執行緒放進 `_UNFINISHED_THREADS` 保留到程式結束

---

## 狀態機設計

```
IDLE ──[開始掃描]──→ SCANNING ──[取消]──→ CANCELING ──[cancelled(scan_id)]──→ IDLE
                       │  ▲                                  （顯示「掃描已取消」）
                   [暫停]  [繼續]
                       ▼  │
                     PAUSED
SCANNING ──[finished(scan_id)]──→ IDLE（顯示結果）
任何狀態 ──[重置／重新開始]──→ _stop_worker() → IDLE（舊掃描的訊號因 scan_id 不符被忽略）
```

| 狀態 | btn_scan | btn_pause | 路徑與掃描設定 |
|------|---------|-----------|---------------|
| IDLE | ▶ 開始掃描 | 隱藏 | 可編輯 |
| SCANNING | ✕ 取消掃描 | ⏸ 暫停 | 停用 |
| PAUSED | ✕ 取消掃描 | ▶ 繼續 | 停用 |
| CANCELING | 取消中…（停用）| 隱藏 | 停用 |

---

## 移除流程

```
勾選 → _delete_selected()
        └─ build_plan()：每個有勾選的群組
             ├─ 找比對基準：保留的檔案中「仍存在且大小、時間沒變」的第一份
             ├─ 整組全選 → whole_group（預設不移除）
             ├─ 已不存在／大小或時間變了／開頭 4 KB 與基準不同 → skip
             ├─ 沒有可用的基準 → 整組 skip
             └─ 位於受保護資料夾或參考資料夾 → protected（預設不移除）
        └─ DeletePlanDialog：預覽、選方式、勾「仍要移除」、匯出 CSV
        └─ RemovalWorker（背景）：可選完整比對 → file_ops.remove_files()
             ├─ recycle：事先判斷進不了回收筒的 → 隔離資料夾；其餘 SHFileOperationW
             └─ quarantine：同碟搬移 + manifest.json
        └─ _apply_removal()：從結果拿掉已移除的檔案（保留展開與捲動位置），不重掃
        └─ journal.jsonl：操作紀錄（HistoryDialog 可還原隔離的檔案）
```

---

## UI 虛擬化結果檢視

- `QTreeWidget` 只繪製可見的列；群組以 `addTopLevelItems` 批次加入；子列在 `itemExpanded` 首次觸發時才建立
- 勾選狀態存在 `FileInfo.selected`，批次操作只改資料再 `refresh_checks()`
- 字型由 `_FontDelegate` 繪製：群組列加粗、大 1px；改字級只切換 `uniformRowHeights` 讓 view 重算列高，不重建
- 副檔名篩選以 300 ms 防抖後才 `populate()`（67,792 個群組時重建一次約 1.2 秒）
- 長路徑 `ElideMiddle`，保留結尾的資料夾名稱

---

## 設定與資料位置

| 位置 | 內容 | 覆寫用環境變數 |
|------|------|---------------|
| `%APPDATA%\DupeScan\DupeScan.ini` | QSettings（INI）| `DUPESCAN_SETTINGS` |
| `%LOCALAPPDATA%\DupeScan\hashcache.sqlite3` | 雜湊快取 | `DUPESCAN_DATA_DIR` |
| `%LOCALAPPDATA%\DupeScan\journal.jsonl` | 操作紀錄 | `DUPESCAN_DATA_DIR` |
| `%LOCALAPPDATA%\DupeScan\reports\` | HTML 報表 | `DUPESCAN_DATA_DIR` |
| `<磁碟根目錄>\.dupescan_quarantine\` | 隔離資料夾（掃描時一律排除）| — |
| `logs\` | 執行日誌 | — |

`DupeScan.ini` 裡的選取規則（`[select]`）：

| 鍵 | 內容 | 寫入時機 |
|----|------|---------|
| `mode`、`conditions` | 操作模式、勾選的條件 id（依優先序）| 關閉視窗 |
| `keep_dirs`、`remove_dirs`、`reference_dirs` | 輸入框原文（`;` 分隔）| 關閉視窗 |
| `custom_rules` | 自訂規則原文（多行、含註解）| 關閉視窗 |
| `presets` | 預設集 JSON（`dump_presets`）| 預設集一有變動就寫入 |
| `preset` | 上次選的預設集名稱 | 關閉視窗 |

---

## 日誌系統

每次啟動產生 `logs/dupescan_YYYYMMDD_HHMMSS.log`，保留最近 30 份。

```
2026-10-20 21:30:00 [INFO ] 掃描開始 — 路徑: D:\Downloads；最小大小: 1,024 B，略過隱藏檔，使用快取
2026-10-20 21:30:00 [INFO ] 磁碟偵測: HDD（單執行緒）
2026-10-20 21:31:12 [INFO ] 掃描統計 — 比對 52,310 個檔案、候選 8,120 個；略過：無法讀取 2、…；快取沿用：前段 8,000、完整 1,950
2026-10-20 21:31:12 [INFO ]   無法讀取: D:\Downloads\locked.bin（另一個程式正在使用此檔案）
2026-10-20 21:31:12 [INFO ] 掃描完成 — 1,061 個重複群組，共浪費 2.4 GiB
2026-10-20 21:32:40 [INFO ] 開始移除 24 個檔案（資源回收筒）
2026-10-20 21:32:41 [INFO ]   移到資源回收筒: D:\Downloads\photo (1).png
2026-10-20 21:32:41 [INFO ] 移除完成 — 資源回收筒 24 個、隔離資料夾 0 個、沒有移除 0 個
2026-10-20 21:32:41 [INFO ] 結果已更新（未重新掃描）— 剩 1,040 個群組
```

---

## 測試

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| 檔案 | 內容 |
|------|------|
| `test_scanner.py` | 重複判定、排序、大小與副檔名篩選、junction／硬連結、排除、隱藏與雲端屬性、取消、暫停真的停、停止不必等大檔、各磁碟執行緒數 |
| `test_hash_cache.py` | 重掃不讀檔、檔案變了重算、改名仍認得、取消後保留進度、演算法變更、舊紀錄清理 |
| `test_selection.py` | 9 種條件與疊加優先序、決定勝負的條件；像副本的檔名（各種標記、不算標記的、空白 vs + 平手）、資料夾優先序（完整路徑／名稱片段、比較深的為準、清單順序）、參考資料夾（保留與刪除模式）、自訂規則（第一條符合的為準、兩種分隔符號、有錯的行）、智慧規則疊加順序、預設集序列化與壞掉的內容 |
| `test_delete_plan.py` | 整組全刪、掃描後變動、保留的那份不見、受保護資料夾、參考資料夾視同受保護 |
| `test_file_ops.py` | 隔離與還原、不覆蓋、回收筒障礙判斷、失敗改走隔離區、使用者取消 |
| `test_results_io.py` | 儲存／開啟結果、CSV |
| `test_ui_smoke.py` | 主視窗 offscreen：掃描、取消、重新開始、重置與關閉不閃退（子程序）、預覽與背景移除、就地更新、鍵盤與 Ctrl＋點擊、掃描設定、多路徑、拖放、記住設定、報表；智慧規則（側邊列、右鍵快速選取、套用側邊列的條件）、參考資料夾（規則與全部勾選略過、📌、預覽視同受保護）、自訂規則有誤時擋下、預設集（另存、取代確認、套用、右鍵套用到一組、刪除、重開還在）、選取規則設定記住 |

隔離：`conftest.py` 把 `DUPESCAN_DATA_DIR`、`DUPESCAN_SETTINGS` 指到暫存資料夾；隔離資料夾在測試中改到 `tmp_path`。pytest 的暫存資料夾位於 `AppData\Local\Temp`，測試期間停用「AppData 受保護」規則。真的送資源回收筒的測試需設定 `DUPESCAN_TEST_RECYCLE=1`。

---

## 效能考量

1. **雜湊快取**：重掃同一個地方時，沒變的檔案完全不讀內容（2 萬個小檔實測 8.79 秒 → 1.05 秒）
2. **Pass 1 不開檔**：`DirEntry.stat()` 直接使用目錄列舉的資料（實測每檔 37.1 µs → 4.1 µs）
3. **(size, partial) 分組**：大小不同但開頭相同的檔案不會進 Pass 3
4. **自適應多執行緒**：依磁碟類型；xxhash 計算時會釋放 GIL，多執行緒有效
5. **在途工作上限**：平行模式不一次建立所有 future
6. **時間節流進度**：`EMIT_INTERVAL` 0.15 秒；Pass 3 以位元組估算 ETA
7. **移除後就地更新**：不必整顆重掃
8. **UI 虛擬化**：見上節
9. **套用選取規則**：約 7 萬個群組／17.6 萬個檔案的合成資料只用「路徑較短」約 0.16 秒，智慧規則與參考資料夾全開約 0.9 秒（不含重繪結果樹），期間顯示等待游標

---

## 關鍵常數

| 常數 | 位置 | 值 | 說明 |
|------|------|----|------|
| `PARTIAL_READ` | scanner.py | 4096 | Pass 2 讀取 bytes 數 |
| `CHUNK_SIZE` | scanner.py | 131072 | Pass 3 每次讀取（128 KB）|
| `EMIT_INTERVAL` | scanner.py | 0.15 | 進度最小間隔（秒）|
| `IN_FLIGHT_PER_WORKER` | scanner.py | 4 | 平行模式每執行緒最多在途工作數 |
| `HASH_ALGO` | scanner.py | `xxh64`／`blake2b` | 寫入快取，變更時舊紀錄作廢 |
| `EXCLUDED_DIR_NAMES` | scanner.py | `$recycle.bin` 等 | 一律不掃描的資料夾 |
| `MAX_ERROR_SAMPLES` | scanner.py | 200 | 無法讀取的明細上限 |
| `PRUNE_AFTER_DAYS` | hash_cache.py | 365 | 快取紀錄保存天數 |
| `FLUSH_EVERY` | hash_cache.py | 2000 | 快取寫入批次 |
| `HEAD_BYTES` | delete_plan.py | 4096 | 移除前比對的開頭長度 |
| `DEFAULT_PROTECTED` | delete_plan.py | fnmatch 樣式 | 受保護資料夾 |
| `RECYCLE_BATCH` | file_ops.py | 50 | 每次送回收筒的檔案數 |
| `MAX_SHELL_PATH` | file_ops.py | 259 | 回收筒支援的最長路徑 |
| `QUARANTINE_DIR_NAME` | file_ops.py | `.dupescan_quarantine` | 隔離資料夾名稱 |
| `KEEP_LOGS` | logger.py | 30 | 保留的 log 份數 |

---

## 歷史問題與修正

### 掃描 Race Condition（v0.2）

舊版 `_on_thread_finished` 無條件清空 `_thread`／`_worker`，掃描結束後立刻重新開始會被舊執行緒的遲到訊號覆蓋。v0.2 以 `_scan_id` 修正；v0.9 起所有 ScanWorker 訊號都帶 `scan_id`。

### 重置閃退（v0.9 修正）

舊版 `_kill_thread()` 等待 5 秒後就丟掉 QThread 參照；若背景還在算大檔（雜湊迴圈不檢查停止），QThread 在執行中被回收，Qt 直接終止程式（`QThread: Destroyed while thread is still running`）。修正：讀檔區塊之間檢查停止、執行緒參照保留到 `finished` 送達才釋放。

### junction 讓同一個檔案變成「兩份重複」（v0.9 修正）

`os.walk(followlinks=False)` 不會跟隨符號連結，但**會走進 junction**（Python 不把 junction 當符號連結）。同一個實體檔經由兩條路徑出現，被分成一組「重複」，刪掉任一份就等於刪掉唯一的一份。修正：遍歷時以 `DirEntry.is_junction()` 略過，並以 `(st_dev, st_ino)` 合併同一實體檔作為最後防線。

### 取消被當成「未發現重複」（v0.9 修正）

舊版停止時 `scan()` 仍呼叫 `on_done([])`，畫面顯示「✅ 未發現重複檔案」。修正：停止改呼叫 `on_cancel()`。

### 「空白 vs +」讓「路徑較短」平手（智慧選取規則）

瀏覽器把檔名的空白換成 `+` 時，`my photo.png` 與 `my+photo.png` 路徑一樣長，「路徑較短」平手，保留哪一份只看列表順序（舊版群組內順序不固定，甚至每次掃描都可能留下不同的那份）。修正：「像副本的檔名優先移除」把 `+`／`%20` 等 URL 編碼痕跡算成複本標記，保留空白那份。
