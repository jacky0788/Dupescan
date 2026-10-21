# TECHNICAL.md — DupeScan 技術參考手冊

> 供技術人員維護、擴充、除錯時參考。對應版本：v1.0.0。

---

## 目錄

- [技術堆疊](#技術堆疊)
- [專案架構](#專案架構)
- [核心模組（不依賴 Qt）](#核心模組不依賴-qt)
- [介面模組](#介面模組)
- [掃描演算法](#掃描演算法)
- [雜湊快取](#雜湊快取)
- [續掃、當機偵測與錯誤處理](#續掃當機偵測與錯誤處理)
- [Threading 模型](#threading-模型)
- [移除流程](#移除流程)
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
| PyQt6 | 6.6+（開發環境 6.11） | GUI（Fusion 樣式＋深色調色盤＋StyleSheet） |
| xxhash | 3.4+ | xxh3_128（備援：hashlib.blake2b，digest 16 bytes） |
| sqlite3 | 內建 | 雜湊快取 |
| ctypes（Win32 API） | 內建 | 磁碟偵測、資源回收筒、程序是否存在 |
| faulthandler | 內建 | 原生層級當機時寫出呼叫堆疊 |
| pytest | 8+（開發用） | 自動化測試 |

---

## 專案架構

```
dupescan/
├── main.py                  # 入口：log → 當機紀錄 → 登記執行中 → 錯誤處理 → AppWindow
├── run.bat                  # 一鍵啟動（.venv）
├── src/
│   ├── __init__.py          # __version__
│   ├── models.py            # FileInfo、DuplicateGroup
│   ├── scan_options.py      # ScanOptions：掃描範圍
│   ├── scanner.py           # 掃描引擎
│   ├── hash_cache.py        # SQLite 雜湊快取
│   ├── disk_detect.py       # 磁碟類型偵測
│   ├── scan_job.py          # 續掃紀錄、上次結果的摘要
│   ├── session.py           # 執行中登記（偵測異常關閉）、faulthandler 當機紀錄
│   ├── selection.py         # 自動選取規則
│   ├── delete_plan.py       # 移除前檢查
│   ├── file_ops.py          # 資源回收筒／隔離資料夾／還原／操作紀錄
│   ├── results_io.py        # 結果存檔、開啟、CSV
│   ├── report.py            # HTML 報表
│   ├── formatting.py        # human_size、format_eta、format_duration、format_time
│   ├── app_paths.py         # %LOCALAPPDATA%\DupeScan
│   ├── logger.py            # 日誌
│   └── ui/
│       ├── app_window.py    # AppWindow：三個畫面、⋯ 選單、流程控制、自動保存
│       ├── home_page.py     # 開始畫面
│       ├── scan_page.py     # 掃描中畫面
│       ├── results_page.py  # 結果畫面（ResultsModel、ResultsView、ResultsDelegate）
│       ├── scan_controller.py # 背景掃描、暫停／停止、續掃紀錄
│       ├── dialogs.py       # 規則、移除確認、設定、文字清單、關於
│       ├── errors.py        # ErrorReporter、ErrorDialog、CrashNoticeDialog
│       ├── history_dialog.py# 操作紀錄與還原
│       ├── removal.py       # 移除（完整比對＋file_ops），在背景執行緒呼叫
│       ├── tasks.py         # run_task：背景執行＋進度視窗
│       ├── app_settings.py  # AppSettings：設定檔讀寫（壞掉的值改回預設）
│       ├── widgets.py       # 晶片、資料夾清單、提示框、浮動訊息、圖片預覽
│       ├── theme.py         # 配色、字級、StyleSheet、圖示
│       └── icons/           # SVG 圖示
└── tests/
```

原則：**掃描、選取、移除檢查、檔案動作、結果存檔、報表都不依賴 Qt**，介面只負責呼叫與顯示。

---

## 核心模組（不依賴 Qt）

### `models.py`

`FileInfo`：`path`、`size`、`hash_full`、`mtime`／`mtime_ns`、`selected`（標成移除）、`dev`／`ino`（只對候選檔查詢；`ino=0` 表示不提供）、`aliases`（硬連結的其他路徑）。`.name`、`.folder`。

`DuplicateGroup`：`hash_value`、`size`（單檔）、`files`（依路徑排序）、`keep_reason`（保留原因，只供顯示）、`.wasted_bytes`＝`size × (n − 1)`。

### `scan_options.py`

```python
ScanOptions(roots, types=[], custom_exts=[], min_size=100 KB, exclude_dirs=[],
            skip_system=True, include_hidden=False)
```

| 方法 | 說明 |
|------|------|
| `extensions()` | 要比對的副檔名集合；`None`＝所有檔案（`types` 與 `custom_exts` 都空） |
| `excluded_names()` | `skip_system` 時為 `SYSTEM_DIR_NAMES`（appdata、.git、node_modules、site-packages、steamapps…，任何位置） |
| `excluded_paths()` | 使用者排除的完整路徑＋`skip_system` 時的系統路徑（由環境變數 SystemRoot、ProgramFiles、ProgramData… 取得） |
| `to_dict()`／`from_dict()` | 續掃紀錄與設定檔用；型別不對的欄位改用預設值 |
| `roots_summary()`／`describe()` | 顯示與 log 用 |

`FILE_TYPES`：圖片、影片、音樂、文件、壓縮檔、AI 模型的副檔名；`SIZE_PRESETS`：0／100 KB／1 MB／10 MB／100 MB；`parse_exts("psd, .AI")` → `["psd", "ai"]`；`file_type_of(name)`。

### `scanner.py`

```python
Scanner(options: ScanOptions, *, cache=None, workers_by_dev=None, pass2_workers=1, pass3_workers=1,
        on_progress=None, on_done=None, on_error=None, on_cancel=None)
```

| 成員 | 說明 |
|------|------|
| `scan()` | 完成 → `on_done(groups, stats)` 並回傳 groups；停止 → `on_cancel()` 回傳 `None`；例外 → `on_error(msg)` 回傳 `None`，完整呼叫堆疊在 `error_details` |
| `stop()`／`pause()`／`resume()` | 任意執行緒呼叫，也可以在 `scan()` 之前 |
| `workers_by_dev` | `{st_dev: (第 2 步, 第 3 步執行緒數)}` |
| `on_progress(msg, cur, total, step, total_steps, eta)` | `total=0`＝還不知道總數（第 1 步）；`eta=-1`＝還無法估算 |
| `stats: ScanStats` | `files_seen`、`candidates`、`same_file_merged`、`skipped_*`、`small_files`、`tail_checked`、`cache_hits_partial`／`cache_hits_full`、`cache_note`、`unreadable`＋最多 200 筆 `unreadable_samples` |

讀檔函式 `_partial_hash`（開頭）、`_tail_hash`（結尾）、`_full_hash`（整檔）：同一種 hasher（所以小檔的開頭 hash 等於完整 hash），整檔讀取用每個執行緒一個 1 MB 緩衝區 `readinto`，每個區塊呼叫一次 `checkpoint(nbytes)`；讀取失敗拋 `OSError`（記入 `unreadable`）。

### `hash_cache.py`

見[雜湊快取](#雜湊快取)。`HashCache(path=None, timeout=10.0)`：`open()` → `lookup(files, algo)` → `store(fi, algo, head=…, tail=…, full=…)` → `close()`，全部在掃描執行緒呼叫；`enabled`、`note`。`cache_key(fi)`、`cache_size()`、`clear_cache()`。

### `disk_detect.py`

`detect_disk_profile(path)` → `DiskProfile`（名稱、第 2／3 步執行緒數）：`CreateFileW`（access=0，不需系統管理員）→ `IOCTL_STORAGE_QUERY_PROPERTY`，SeekPenalty 區分 HDD／SSD，BusType 區分 NVMe／SATA。

| 磁碟類型 | 第 2 步 | 第 3 步 |
|---------|--------|--------|
| NVMe SSD | 8 | 4 |
| SATA SSD／其他 SSD | 4 | 2 |
| HDD | 1 | 1 |
| 未知／網路 | 2 | 1 |

### `scan_job.py`

| 名稱 | 說明 |
|------|------|
| `ScanJob(options, state, started_at, updated_at, step, step_progress, error, pid)` | `state`：running（進行中或當機）／stopped／failed；`progress_text()`→「停在第 3 步（63%）」 |
| `save_job()`／`load_job()`／`clear_job()` | `scan_job.json`（先寫 .tmp 再 `os.replace`）；內容壞掉時 `load_job()` 回傳 `None` 並刪檔 |
| `last_results_path()` | `last_results.dupescan` |
| `save_last_summary()`／`load_last_summary()` | `last_results.json`：資料夾、時間、組數、可釋放空間（開始畫面顯示）；結果檔不在時回傳 `None` |

### `session.py`

| 名稱 | 說明 |
|------|------|
| `start_session(crash_log)` | 在 `sessions\<pid>.lock` 登記這次執行；回傳之前沒有正常關閉的執行（lock 還在、程序已不存在） |
| `end_session()` | 正常結束時刪掉自己的 lock |
| `pid_alive(pid)` | Windows：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`＋`GetExitCodeProcess`（`STILL_ACTIVE`）；拒絕存取視為存在 |
| `enable_crash_log(log_dir)` | `faulthandler.enable(file=crash_<時間>_<pid>.log, all_threads=True)`；清掉以前空的紀錄，有內容的只留 10 份 |
| `read_crash_log(path)` | 最後 4000 字 |

### `selection.py`

| 名稱 | 說明 |
|------|------|
| `Condition(id, keys)` | `keys(files)` 算出每個檔案的分數，**越低越優先保留** |
| `condition(id)` | `older`、`newer`、`sh_path`、`copy_name` |
| `folders_condition(remove_dirs)` | 在優先移除的資料夾為正（清單越前面越大） |
| `pick_keeper(files, conditions, candidates=None)` | 逐一比較，每個條件留下分數最低的候選，平手才看下一個；回傳（索引, 決定的條件 id）；全部平手取第一個、決定者 `None` |
| `apply_selection(group, conditions, protected=None)` | 更新 `selected`；保護的資料夾裡的檔案一律保留，群組裡有保護的檔案時保留的那份從它們之中選；回傳 `"protected"`／條件 id／`None` |
| `AutoRules(copy_names, protected_dirs, remove_dirs, fallback)` | `conditions()`：優先移除的資料夾 → 原始檔名 → 其他情況（`FALLBACKS`）；`to_dict()`／`from_dict()` |
| `auto_select(groups, rules)` | 套用到每一組並寫入 `keep_reason`（`REASONS`），回傳各原因的組數 |

**原始檔名**：`copy_info(name)` →（去掉複本標記後的檔名, 標記數）。標記：接在任何一個 `.` 之前或檔名結尾的 ` (1)`～` (99)`（含全形括號；`(0)`、`(100)`、`(2019)` 不算）、` - 複製／复制／副本／コピー／Copy`（可再接 ` (2)`）、macOS ` copy 2`、Linux ` (copy)`、`.dup1`；開頭的 `Copy of `；檔名含 `+`、`%` 時先 `unquote_plus`，有變化算 1 個標記。`copy_scores(files)`：同名（去掉標記後）的檔案中比最少的多幾個標記；群組裡沒有同名的其他檔案時為 0。

**資料夾清單** `FolderList(dirs)`：不分大小寫、`\` 換成 `/`，只比對檔案所在的資料夾。完整路徑（`x:` 或 UNC）＝資料夾本身與所有子資料夾；其他視為名稱片段（路徑中任何位置含有就算；`\old\` 要整個名稱相同）。

### `delete_plan.py`

`build_plan(groups, protected=None, verify_head=True, reference_dirs=None)`，`groups` 為 `(編號, DuplicateGroup)`，只處理有標成移除的群組。

| 狀態 | 條件 |
|------|------|
| `keep` | 保留的檔案 |
| `skip` | 已不存在、大小或修改時間變了、開頭 4 KB 和「保留的那份」不同、沒有可用的保留那份 |
| `protected` | `DEFAULT_PROTECTED`（系統與程式資料夾，`PROTECTED_NOTE`）或 `reference_dirs`（保護的資料夾，`REFERENCE_NOTE`）；預設不移除 |
| `ok` | 會移除 |

`DeletePlan.removable(allow_whole_group, allow_protected)`；`whole_groups`（整組都標成移除，新介面不處理）。

### `file_ops.py`

`remove_files(items, method, hwnd, progress)`：`recycle` → `SHFileOperationW(FO_DELETE)`，旗標 `FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT | FOF_WANTNUKEWARNING`，每 50 個一批，失敗時逐一重試；`recycle_obstacle()` 事先判斷進不了回收筒的（路徑 > 259、網路／卸除式、`NukeOnDelete`、超過 `MaxCapacity`）改走隔離資料夾。`quarantine` → `<磁碟根目錄>\.dupescan_quarantine\<batch>\<原路徑>`＋`manifest.json`；`restore_batch()` 不覆蓋既有檔案；每次操作寫一行 `journal.jsonl`。

### `results_io.py`、`report.py`、`formatting.py`

- `save_results(path, groups, roots, scanned_at, scan=None)`：gzip JSON（`format="dupescan-results"`、`version=1`），每組含 `reason`；`scan` 存掃描設定與統計（`ResultsInfo.to_scan_dict()`）。`load_results()` 回傳 `(groups, meta)`，`meta["scan"]` 一定是 dict；格式不對拋 `ValueError`
- `export_csv()`：UTF-8 BOM；欄位：群組、處理（保留／移除）、路徑、資料夾、檔名、大小、修改時間、保留原因、這組可釋放、硬連結路徑、hash
- `report.build_report()`／`save_report()`：`html.escape` 所有路徑；圓餅圖最多 10 塊（其餘合併「其他」）；報表只留 20 份

---

## 介面模組

### 畫面與流程（`app_window.py`）

```
開始 ──[開始掃描／繼續掃描]──→ 掃描中 ──[完成]──→ 結果（自動套用規則並保存）
  ▲                              │ [停止]──────────────→ 開始（顯示「上次的掃描沒有完成」）
  │                              │ [出錯]──→ 錯誤說明 ──[重試]──→ 掃描中（繼續）
  └──────────[回到開始]──────────┴──────────────────────[回到開始]
結果 ──[重新掃描]──→ 掃描中
```

`AppWindow(settings=None, crashed=None)`：上方細標題列（DupeScan、版本、⋯）、`QStackedWidget`（HomePage／ScanPage／ResultsPage）、`Toast`。`startup_notices()` 在視窗顯示後呼叫（`QTimer.singleShot(0)`）：上次沒有正常關閉 → `CrashNoticeDialog`；設定檔有問題 → 浮動訊息。

自動保存：開始畫面的變更 1 秒後寫設定；保留／移除的變更 3 秒後、移除後、開始新掃描前、關閉時寫 `last_results.dupescan`。另一個 DupeScan 正在執行的續掃紀錄（state=running 且 pid 存在）不顯示。

### 開始畫面（`home_page.py`）

`set_options(ScanOptions)`／`options()`；`set_job(ScanJob | None)`、`set_last_summary(dict | None)`。開始前用 `folder_problem()` 檢查每個資料夾（找不到、不是資料夾、沒有權限），有問題的列標紅、不開始。類型晶片 `ChipGroup(all_key="all")`：選「全部」取消其他，其他都沒選時回到「全部」；「自訂…」顯示副檔名輸入框（沒填時擋下）。

### 掃描中畫面（`scan_page.py`）

`start(options, resumed)`、`update_progress(Progress)`（第 1 步顯示忙碌動畫）、`set_paused()`、`set_stopping()`、`show_error(message)`。

### 掃描控制（`scan_controller.py`）

`ScanController`：`start(options, resumed)`、`pause()`／`resume()`／`stop()`、`shutdown(wait_ms)`；signals：`started`、`progress(Progress)`、`disk_detected`、`paused_changed`、`finished(groups, ScanSummary)`、`stopped`、`failed(message, details)`。

- 開始時寫續掃紀錄，之後每 2 秒更新步驟與比例；完成時清除；停止標 stopped；出錯標 failed（含訊息）
- 每次掃描一個 `scan_id`，舊掃描遲到的訊號一律忽略
- 上一次還在收尾時再開始：排隊（`_pending`），等執行緒結束才開始，不會有兩個掃描同時用快取
- `_Worker.run()`：偵測各磁碟類型（失敗就用預設）→ `Scanner.scan()`；任何例外都轉成 `failed`
- `worker.ended → thread.quit` 用 **DirectConnection**（QThread.quit 是執行緒安全的），主執行緒在 `shutdown()` 裡 `wait()` 時也能結束
- `shutdown()` 等不到時把 `(thread, worker)` 放進 `_UNFINISHED`；`main.py` 結束前 `wait_unfinished()`，仍未結束就 `os._exit()`，避免 Qt 在收尾時因為執行緒還在跑而閃退

### 結果畫面（`results_page.py`）

- `ResultsModel(QAbstractItemModel)`：兩層（群組 → 檔案），子項的 internalPointer 是所屬群組；`_rows` 是符合搜尋與類型、排好序的群組。`state(fi)`：keep／remove／protected；`toggle()` 回傳 `"ok"`／`"protected"`／`"last"`（這組只剩這份保留）；`keep_only()`、`keep_all()`、`reselect()`、`apply_rules()`、`remove_paths()`、`totals()`。保護的資料夾裡的檔案載入時一律改成保留
- `ResultsView(QTreeView)`：`uniformRowHeights`；群組列在 `drawRow()` 整列自己畫（背景、展開箭頭、檔名、份數、這組會釋放多少）；點膠囊（第一欄）或空白鍵切換、點群組列展開／收合（點在展開箭頭上時不重複切換）、雙擊開啟、右鍵選單、圖片縮圖
- `ResultsDelegate`：檔案列第一欄畫膠囊
- 收合的群組記在 `_collapsed`（依群組 id），重新整理（`expandAll()` 會發出 expanded signal，期間不記錄）後維持收合
- `ResultsInfo`：資料夾、掃描時間、設定、檔案數、費時、無法讀取的檔案、快取狀況、來源檔；`from_summary()`／`from_meta()`／`to_scan_dict()`／`rescan_options()`

### 對話框、背景工作與錯誤

- `RulesDialog(rules)` → `rules()`；`RemoveDialog(plan, method, full_verify)` → `selected()`＝`[(FileInfo, 保留的那份的路徑)]`；`SettingsDialog(settings, cache_size, scanning)` → `values()`、`cache_cleared`；`TextDialog`、`unreadable_dialog()`、`about()`
- `tasks.run_task(parent, title, text, fn)`：`fn(report)` 在 `QThread` 執行，主執行緒跑 `QEventLoop` 並顯示模態進度視窗；所有回報都用 QueuedConnection 回到主執行緒；例外轉成 `TaskError(error, details)`
- `removal.remove_items(items, method, hwnd, full_verify, report)`：完整比對時重新計算「保留的那份」與要移除的檔案再互相比對（不依賴掃描時的 hash，舊版本存的結果檔也能用）→ `file_ops.remove_files()`
- `errors.ErrorReporter.install()`：`sys.excepthook`、`threading.excepthook`（背景執行緒）、`qInstallMessageHandler`（Qt 警告寫 log）；例外寫 log 後經 QueuedConnection 回主執行緒顯示 `ErrorDialog`（非模態；視窗開著時又出錯就累計次數、附上內容）。`describe()` 把 MemoryError、PermissionError、OSError 換成白話
- `app_settings.AppSettings`：每個值讀取時檢查，讀不懂就移除該鍵、用預設值並設定 `notice`；`sync()` 寫不進去時也設定 `notice`。沒有 `scan/options` 時從 0.9 版的設定搬過資料夾清單、副檔名、隱藏檔與規則用的資料夾

### 主題（`theme.py`）

`apply(app, px)`：Fusion＋`palette()`＋`stylesheet(px)`，字型 Microsoft JhengHei UI；`QDir.setSearchPaths("icons", …)` 讓 StyleSheet 用 `url(icons:check.svg)`。文字角色用 QLabel 的 `role` 屬性（title、heading、section、muted、faint、error、warning、success），卡片與提示框用 QFrame 的 `card`／`banner` 屬性，按鈕用 objectName（primary、danger、link、icon、chip、expander）。`set_role()` 會重新套用樣式。

---

## 掃描演算法

```
所有檔案
    │
    ▼ 第 1 步：os.scandir 遍歷，DirEntry.stat() 取大小（不開檔），依 size 分組
    │  ── 略過：符號連結、junction／掛載點、EXCLUDED_DIR_NAMES、排除與系統資料夾、隱藏、雲端佔位檔、小於門檻
    │  ── 刪除 size 唯一的 key；候選 os.stat 取 (st_dev, st_ino)，合併同一實體檔（硬連結）
    │
    ▼ 快取查詢：大小、修改時間（ns）、HASH_ALGO 都相同 → 直接沿用 head／tail／full
    │
    ▼ 第 2 步：開頭 4 KB → 以 (size, head) 分組
    │  ── size ≤ 4 KB：head 就是完整 hash，直接成組（不再讀）
    │  ── size ≥ 8 MB 且沒有完整 hash 快取：再讀結尾 4 KB → 以 (size, 群組, tail) 分組
    │
    ▼ 第 3 步：整檔 xxh3_128 → 以 (size, full) 分組
    │
    └─→ list[DuplicateGroup]：群組內依路徑排序，群組依 wasted_bytes 由大到小
```

**讀檔排程**（`_hash_stage`）：依 `fi.dev` 分批，每顆磁碟一個 `ThreadPoolExecutor`（執行緒數由磁碟類型決定）與在途上限（執行緒數 × 4）；單執行緒的磁碟依路徑排序（同資料夾的檔案在磁碟上通常相鄰）。所有磁碟的工作一起送出，用 `wait(FIRST_COMPLETED, timeout=0.15 s)` 收結果，順便發送進度。每個結果先交給 `store`（寫進快取）；停止時已經算完、還沒交回的結果也會存起來。

**暫停與停止**：`_checkpoint()` 在每個讀檔區塊之間呼叫（worker 執行緒也一樣）。`stop()` 必須同時 set pause_event，否則暫停中的執行緒收不到停止訊號。

---

## 雜湊快取

- 位置：`%LOCALAPPDATA%\DupeScan\hashcache.sqlite3`（WAL）；`PRAGMA user_version = 2`，版本不同時重建資料表
- 資料表 `files(key, size, mtime_ns, algo, head, tail, full, seen)`
- **鍵**：本機 NTFS／ReFS 用 `i:<dev>:<ino>`（改名、搬移仍認得）；FAT／exFAT、網路磁碟的檔案 ID 可能被重複使用，用 `p:<normcase(路徑)>`
- **有效條件**：`size`、`mtime_ns`、`algo`（`xxh3_128/h4096/t4096`，含取樣長度）都相同
- **寫入**：每 2000 筆或距離上次寫入超過 2 秒就寫入；停止、出錯時 `close()` 也會寫入
- **出狀況**：檔案損壞 → 改名 `.bad-<時間>` 後重建（`note` 註明）；被另一個連線鎖住超過逾時 → 這次不使用快取；讀寫時出錯 → 之後這次掃描不使用快取
- **清理**：`seen` 超過 365 天的紀錄在掃描結束時刪除
- 風險：內容改了但大小與修改時間都沒變（極少見）→ 由移除前的開頭 4 KB 複驗與可選的完整比對把關

---

## 續掃、當機偵測與錯誤處理

| 機制 | 位置 | 說明 |
|------|------|------|
| 續掃紀錄 | `scan_job.json` | 掃描開始寫入、每 2 秒更新、完成時刪除；停止／出錯／當機都會留下。繼續＝同樣設定重跑，已算好的 hash 都在快取裡 |
| 上次的結果 | `last_results.dupescan`＋`last_results.json` | 掃描完成、選擇變更（3 秒後）、移除後、關閉時寫入 |
| 異常關閉偵測 | `sessions\<pid>.lock` | 啟動時登記、正常結束時刪除；下次啟動發現 lock 在、程序不在 → 提示 |
| 當機紀錄 | `logs\crash_<時間>_<pid>.log` | faulthandler；沒有當機時是空檔，下次啟動清掉 |
| 未處理的例外 | `ErrorReporter` | 寫 log、顯示錯誤視窗，程式繼續 |
| 啟動失敗 | `main.py` | 寫 log、`QMessageBox.critical` 說明、結束碼 1 |
| 掃描錯誤 | `ScanController.failed` | 掃描畫面顯示錯誤與重試；呼叫堆疊寫 log、可在「詳細內容」查看 |
| 背景工作錯誤 | `run_task` → `TaskError` | 顯示說明與詳細內容，檔案沒有被更動 |

---

## Threading 模型

```
Main Thread（Qt 事件迴圈）
  ├─ QThread「掃描」── _Worker.run() ── Scanner.scan()
  │                                      └─ 每顆磁碟一個 ThreadPoolExecutor（第 2／3 步）
  └─ QThread「背景工作」── run_task：建立移除計畫、移除檔案（主執行緒在 QEventLoop 等待）

Worker → Main：Qt signals（掃描訊號都帶 scan_id）
Main → Worker：Scanner 的 threading.Event（stop／pause）
```

- sqlite3 連線只在掃描執行緒使用（`HashCache.open()` 在 `Scanner._run()` 裡呼叫）
- QThread 與 worker 的參照保留到 `thread.finished` 送達才 `deleteLater()`；執行中的 QThread 被回收會讓 Qt 直接終止程式
- 測試結束時 `conftest.py` 確定地關閉並刪除所有頂層視窗，避免殘留的視窗在之後某次處理事件時才被垃圾回收而崩潰

---

## 移除流程

```
「移到資源回收筒」→ AppWindow._remove_selected()
   └─ run_task：build_plan()
        ├─ 比對基準：保留的檔案中「仍存在且大小、時間沒變」的第一份
        ├─ 已不存在／大小或時間變了／開頭 4 KB 與基準不同 → skip
        ├─ 沒有可用的基準 → 整組 skip
        └─ 系統或程式資料夾、保護的資料夾 → protected（預設不移除）
   └─ RemoveDialog：摘要、需要注意的事項、「查看清單」、勾「仍要移除這些檔案」
   └─ run_task：removal.remove_items()
        ├─ 完整比對（設定開啟時）：重新計算保留的那份與每個要移除的檔案，不同的略過
        └─ file_ops.remove_files()：recycle（進不了回收筒的改走隔離）／quarantine
   └─ ResultsPage.files_removed()：拿掉已移除的檔案，剩一份的群組不再列出；保存結果
   └─ 浮動訊息：移了幾個、釋放多少；有沒移除的可以「查看」原因
```

---

## 設定與資料位置

| 位置 | 內容 | 覆寫用環境變數 |
|------|------|---------------|
| `%APPDATA%\DupeScan\DupeScan.ini` | 設定 | `DUPESCAN_SETTINGS` |
| `%LOCALAPPDATA%\DupeScan\` | 快取、續掃紀錄、上次結果、操作紀錄、報表、sessions | `DUPESCAN_DATA_DIR` |
| `logs\`（專案資料夾） | 執行紀錄與當機紀錄 | `DUPESCAN_LOG_DIR` |
| `<磁碟根目錄>\.dupescan_quarantine\` | 隔離資料夾（掃描時一律排除） | — |

`DupeScan.ini`：

| 鍵 | 內容 |
|----|------|
| `scan/options` | `ScanOptions.to_dict()` 的 JSON |
| `select/rules` | `AutoRules.to_dict()` 的 JSON |
| `view/text_size` | small／normal／large／xlarge |
| `view/result_sort` | wasted／count／name |
| `remove/method` | recycle／quarantine |
| `remove/full_verify` | true／false |
| `window/geometry` | 視窗位置與大小 |

---

## 日誌系統

每次啟動產生 `logs/dupescan_YYYYMMDD_HHMMSS.log`（保留 30 份）。記錄：啟動版本、上次沒有正常關閉、掃描設定、磁碟類型、完成時的統計（檔案數、候選、快取沿用、各種略過）、快取狀況、無法讀取的檔案（前 50 筆）、暫停／停止、錯誤與呼叫堆疊、規則變更、每個被移除或沒有移除的檔案與原因、開啟的檔案與結果檔、Qt 警告。

```
2026-10-21 14:03:10 [INFO ] DupeScan 1.0.0 啟動（Python 3.14.3）
2026-10-21 14:03:15 [INFO ] 開始掃描 — 路徑: D:\Photos; E:\Backup，類型: 全部，略過小於: 100 KB，略過系統資料夾，略過隱藏檔
2026-10-21 14:03:15 [INFO ] 磁碟：D: NVMe SSD（…） · E: HDD（…）
2026-10-21 14:04:35 [INFO ] 掃描完成 — 128 組重複，最多可釋放 3,612,345,678 bytes，費時 80.2 秒
2026-10-21 14:06:02 [INFO ] 移除 24 個檔案（recycle）
2026-10-21 14:06:03 [INFO ]   回收筒：D:\Downloads\photo (1).jpg
2026-10-21 14:06:03 [INFO ] 移除完成：成功 24、沒有移除 0
```

---

## 測試

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| 檔案 | 內容 |
|------|------|
| `test_scanner.py` | 重複判定、檔案類型、大小門檻與空檔、排除資料夾、略過系統資料夾、junction／硬連結、隱藏與雲端屬性、停止與暫停、各磁碟執行緒數、小檔只讀一次、檔尾取樣、HDD 依路徑順序、多顆磁碟同時進行 |
| `test_hash_cache.py` | 重掃不讀檔、檔案變了重算、改名仍認得、停止後保留進度、演算法變更、舊紀錄清理、快取損壞重建、被鎖住時不使用、定時寫入 |
| `test_scan_job.py` | 續掃紀錄存取與壞檔、上次結果摘要、pid 是否存在、異常關閉偵測、原生當機紀錄（子程序） |
| `test_selection.py` | 條件與平手、原始檔名（各種標記）、資料夾比對、優先移除的資料夾、保護的資料夾、AutoRules 與 auto_select |
| `test_delete_plan.py` | 整組全刪、掃描後變動、保留的那份不見、受保護與保護的資料夾 |
| `test_file_ops.py` | 隔離與還原、不覆蓋、回收筒障礙判斷、失敗改走隔離區、使用者取消 |
| `test_results_io.py`、`test_formatting_report.py` | 結果存檔與 CSV；格式化函式、報表統計、跳脫、圓餅圖、只留 20 份 |
| `test_ui_start_scan.py` | 設定檔（預設、壞掉的值、舊版搬移）、開始畫面（類型晶片、自訂副檔名、資料夾檢查與合併、續掃提示）、掃描中畫面、ScanController（完成、停止、上一次收尾中再開始、錯誤、關閉時暫停中） |
| `test_ui_results.py` | 膠囊與每組至少保留一份、保護的檔案、只保留這一份／全部保留／重新選取、搜尋類型排序、移除後更新、點擊與鍵盤、收合維持、結果資訊存檔 |
| `test_ui_window.py` | 主視窗：掃描到結果、停止與繼續、出錯重試、移除（隔離）、完整比對略過變動的檔案、規則、設定、重開後的上次結果、關閉時保留續掃紀錄、當機提示、錯誤視窗（含背景執行緒）、run_task、沒有重複、拖放、匯出與開啟結果 |
| `test_app_lifecycle.py` | 子程序：`main.py` 正常啟動與結束、啟動失敗、掃描中被強制結束後可以繼續、關閉時掃描執行緒卡住不閃退 |

隔離：`conftest.py` 把 `DUPESCAN_DATA_DIR`、`DUPESCAN_SETTINGS`、`DUPESCAN_LOG_DIR` 指到暫存資料夾（子程序也會繼承）；隔離資料夾在測試中改到 `tmp_path`。pytest 的暫存資料夾位於 `AppData\Local\Temp`，測試期間停用「AppData 受保護」規則。真的送資源回收筒的測試需設定 `DUPESCAN_TEST_RECYCLE=1`。

---

## 效能考量

1. **雜湊快取**：重掃同一個地方時，沒變的檔案完全不讀內容
2. **第 1 步不開檔**：`DirEntry.stat()` 直接使用目錄列舉的資料（每檔約 37 µs → 4 µs）
3. **小檔只讀一次**：2 萬個小檔實測 4.4 秒 → 1.9 秒
4. **檔尾取樣**：開頭相同的大檔（同格式的影片、模型）先比結尾 4 KB，實測整檔讀取量少 2/3
5. **磁碟排程**：每顆磁碟各自的執行緒池；HDD 依路徑順序；多顆磁碟同時進行
6. **緩衝區重複使用**：整檔讀取 `readinto` 同一個 1 MB 緩衝區
7. **時間節流進度**：0.15 秒；第 3 步以位元組估算 ETA
8. **結果清單**：自訂 model＋`uniformRowHeights`，只畫看得到的列；兩萬組約 0.4 秒顯示完成，切換一份約 15 ms
9. **移除後就地更新**：不必重掃

---

## 關鍵常數

| 常數 | 位置 | 值 | 說明 |
|------|------|----|------|
| `HEAD_READ`／`TAIL_READ` | scanner.py | 4096 | 開頭／結尾取樣長度 |
| `TAIL_MIN_SIZE` | scanner.py | 8 MB | 這麼大以上才比對結尾 |
| `CHUNK_SIZE` | scanner.py | 1 MB | 整檔讀取區塊 |
| `HASH_ALGO` | scanner.py | `xxh3_128/h4096/t4096` | 寫入快取，變更時舊紀錄作廢 |
| `EMIT_INTERVAL` | scanner.py | 0.15 | 進度最小間隔（秒） |
| `IN_FLIGHT_PER_WORKER` | scanner.py | 4 | 每執行緒最多在途工作數 |
| `EXCLUDED_DIR_NAMES` | scanner.py | `$recycle.bin` 等 | 一律不掃描的資料夾 |
| `MAX_ERROR_SAMPLES` | scanner.py | 200 | 無法讀取的明細上限 |
| `DEFAULT_MIN_SIZE` | scan_options.py | 100 KB | 預設略過的大小 |
| `SYSTEM_DIR_NAMES` | scan_options.py | appdata、.git… | 略過系統與程式資料夾 |
| `FLUSH_EVERY`／`FLUSH_SECONDS` | hash_cache.py | 2000／2.0 | 快取寫入時機 |
| `PRUNE_AFTER_DAYS` | hash_cache.py | 365 | 快取紀錄保存天數 |
| `SAVE_JOB_INTERVAL_MS` | scan_controller.py | 2000 | 續掃紀錄更新間隔 |
| `SAVE_RESULTS_DELAY_MS` | app_window.py | 3000 | 選擇變更後多久保存結果 |
| `KEEP_CRASH_LOGS` | session.py | 10 | 保留的當機紀錄 |
| `HEAD_BYTES` | delete_plan.py | 4096 | 移除前比對的開頭長度 |
| `DEFAULT_PROTECTED` | delete_plan.py | fnmatch 樣式 | 系統與程式資料夾 |
| `RECYCLE_BATCH` | file_ops.py | 50 | 每次送回收筒的檔案數 |
| `MAX_SHELL_PATH` | file_ops.py | 259 | 回收筒支援的最長路徑 |
| `KEEP_REPORTS` | report.py | 20 | 保留的報表份數 |
| `KEEP_LOGS` | logger.py | 30 | 保留的 log 份數 |

---

## 歷史問題與修正

### 掃描 Race Condition（v0.2）

掃描結束後立刻重新開始，會被舊執行緒的遲到訊號覆蓋。以 `scan_id` 修正；v1.0 起上一次還在收尾時，新的掃描排隊等它結束。

### 重置閃退（v0.9）

QThread 在執行中被回收，Qt 直接終止程式（`QThread: Destroyed while thread is still running`）。修正：讀檔區塊之間檢查停止、執行緒參照保留到 `finished` 送達；v1.0 起關閉時仍未結束就 `os._exit()`。

### junction 讓同一個檔案變成「兩份重複」（v0.9）

`os.walk(followlinks=False)` 會走進 junction。修正：遍歷時略過 junction，並以 `(st_dev, st_ino)` 合併同一實體檔。

### 取消被當成「未發現重複」（v0.9）

停止時改呼叫 `on_cancel()`，不再呼叫 `on_done([])`。

### 關閉視窗要等逾時（v1.0）

`worker.ended → thread.quit` 是跨執行緒的 QueuedConnection：主執行緒在 `wait()` 裡等執行緒結束，`quit` 卻排在主執行緒的事件佇列裡，只能等到逾時。改用 DirectConnection。

### 測試偶發崩潰（v1.0）

殘留的視窗被 lambda 連線的循環參照留住，之後某次 `processEvents()` 時才被垃圾回收，刪到正在處理事件的物件。修正：每個測試結束時確定地關閉並刪除所有頂層視窗。

### 「空白 vs +」讓「路徑最短」平手

`my photo.png` 與 `my+photo.png` 一樣長。「保留原始檔名」把 `+`／`%20` 算成複本標記，保留空白那份。
