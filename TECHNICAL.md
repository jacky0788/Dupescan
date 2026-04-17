# TECHNICAL.md — DupeScan 技術參考手冊

> 供技術人員維護、擴充、除錯時參考。

---

## 目錄

- [技術堆疊](#技術堆疊)
- [專案架構](#專案架構)
- [模組說明](#模組說明)
- [掃描演算法](#掃描演算法)
- [Threading 模型](#threading-模型)
- [狀態機設計](#狀態機設計)
- [UI 分批渲染](#ui-分批渲染)
- [日誌系統](#日誌系統)
- [效能考量](#效能考量)
- [關鍵常數](#關鍵常數)

---

## 技術堆疊

| 技術 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 主要語言 |
| PyQt6 | 6.6+ | GUI 框架（視窗、元件、事件迴圈）|
| xxhash | 3.4+ | 高速 Hash 演算法（備援：hashlib.blake2b）|
| humanize | 4.9+ | 人性化顯示檔案大小（如 1.2 MiB）|

---

## 專案架構

```
dupescan/
├── main.py              # 入口：建立 QApplication 與 MainWindow
├── run.bat              # 一鍵啟動腳本（自動安裝依賴）
├── requirements.txt     # pip 依賴清單
├── .gitignore
├── README.md            # 使用者操作說明
├── TECHNICAL.md         # 本檔案（技術手冊）
├── logs/                # 執行日誌，每次啟動產生新檔案（gitignore）
└── src/
    ├── __init__.py
    ├── models.py        # 資料結構定義
    ├── scanner.py       # 掃描引擎（3-pass + 多執行緒）
    ├── disk_detect.py   # 磁碟類型偵測（ctypes，決定執行緒策略）
    ├── logger.py        # 日誌設定（單例）
    └── ui/
        ├── __init__.py
        └── main_window.py   # 主視窗（Qt UI + 事件邏輯）
```

---

## 模組說明

### `main.py`

| 函式 | 說明 |
|------|------|
| `main()` | 建立 `QApplication`、`MainWindow`，進入 Qt 事件迴圈 |

---

### `src/models.py`

#### `FileInfo`

| 欄位 | 型別 | 說明 |
|------|------|------|
| `path` | `Path` | 檔案絕對路徑 |
| `size` | `int` | 檔案大小（bytes）|
| `hash_full` | `str` | 完整 Hash 值（Pass 3 後填入）|
| `mtime` | `float` | 修改時間（Unix timestamp）|
| `.name` | property | `path.name`（檔名）|
| `.folder` | property | `str(path.parent)`（所在目錄）|

#### `DuplicateGroup`

| 欄位 | 型別 | 說明 |
|------|------|------|
| `hash_value` | `str` | 群組共同 Hash 值 |
| `size` | `int` | 單一檔案大小（bytes）|
| `files` | `list[FileInfo]` | 同一群組的所有重複檔案 |
| `.wasted_bytes` | property | `size × (len(files) - 1)`（可釋放空間）|

---

### `src/disk_detect.py`

磁碟類型偵測模組。掃描開始前由 `ScanWorker.run()` 呼叫，結果決定 Scanner 的執行緒數量。

#### `class DiskProfile`

| 欄位 | 型別 | 說明 |
|------|------|------|
| `type_name` | `str` | 顯示名稱（"NVMe SSD" / "SATA SSD" / "HDD" / "未知磁碟"）|
| `pass2_workers` | `int` | Pass 2 執行緒數 |
| `pass3_workers` | `int` | Pass 3 執行緒數 |
| `summary()` | method | 回傳 UI 顯示字串 |

**預設策略對照：**

| 磁碟類型 | Pass 2 threads | Pass 3 threads |
|---------|---------------|---------------|
| NVMe SSD | 8 | 4 |
| SATA SSD | 4 | 2 |
| HDD | 1 | 1 |
| 未知/網路 | 2 | 1 |

#### `detect_disk_profile(path: Path) -> DiskProfile`

入口函式。任何例外（網路磁碟、虛擬磁碟、非 Windows）自動 fallback 到保守值，不影響掃描主流程。

**Windows 偵測流程（純 ctypes，無需管理員權限）：**

```
path → drive letter → \\.\C: 裝置路徑
    │
    ├─ IOCTL_STORAGE_QUERY_PROPERTY
    │   PropertyId=7 (StorageDeviceSeekPenaltyProperty)
    │   → IncursSeekPenalty=True  → HDD
    │   → IncursSeekPenalty=False → SSD (繼續查 BusType)
    │
    └─ IOCTL_STORAGE_QUERY_PROPERTY
        PropertyId=0 (StorageDeviceProperty)
        → BusType=17 (NVMe) → NVMe SSD
        → BusType=11 (SATA) → SATA SSD
        → 其他           → SSD (generic)
```

**關鍵內部函式：**

| 函式 | 說明 |
|------|------|
| `_volume_device_path(path)` | `path.resolve().drive` → `\\\\.\\C:` |
| `_open_volume(device)` | `CreateFileW` 開啟磁碟卷（0 access，僅供 IOCTL）|
| `_ioctl(h, ctl, in, out)` | 包裝 `DeviceIoControl` 呼叫，回傳 bool |
| `_query_seek_penalty(device)` | SeekPenalty 查詢，回傳 `True/False/None` |
| `_query_bus_type(device)` | BusType 查詢，回傳 `'nvme'/'sata'/'other'` |

---

### `src/scanner.py`

#### 模組層級常數

| 常數 | 值 | 說明 |
|------|----|------|
| `PARTIAL_READ` | `4096` | Pass 2 讀取的前幾 bytes |
| `CHUNK_SIZE` | `65536` | Pass 3 每次讀取的 chunk 大小 |
| `TOTAL_STEPS` | `3` | 掃描步驟總數 |
| `STEP_NAMES` | dict | `{1: "依檔案大小分組", 2: "快速 Hash...", 3: "完整 Hash..."}` |
| `_FAST_HASH` | bool | `True` 若 xxhash 已安裝，否則 `False`（退回 blake2b）|

#### 模組層級函式

| 函式 | 簽名 | 說明 |
|------|------|------|
| `_format_eta` | `(seconds: int) -> str` | 將秒數格式化為中文時間（"約 X 分 Y 秒"）|
| `_partial_hash` | `(path: Path) -> str \| None` | 讀取前 `PARTIAL_READ` bytes 計算 hash，IO 失敗回傳 None |
| `_full_hash` | `(path: Path) -> str \| None` | 讀取整個檔案計算 hash，IO 失敗回傳 None |

#### `class Scanner`

**建構子參數：**

| 參數 | 型別 | 說明 |
|------|------|------|
| `roots` | `list[str]` | 要掃描的根路徑清單 |
| `min_size` | `int` | 最小檔案大小（bytes），小於此值的檔案跳過 |
| `include_hidden` | `bool` | 是否包含隱藏檔案/目錄（預設 False）|
| `pass2_workers` | `int` | Pass 2 執行緒數（預設 1，由 DiskProfile 提供）|
| `pass3_workers` | `int` | Pass 3 執行緒數（預設 1，由 DiskProfile 提供）|
| `on_progress` | `ProgressCallback \| None` | 進度回調，簽名見下方 |
| `on_done` | `Callable[[list[DuplicateGroup]], None] \| None` | 掃描完成回調 |
| `on_error` | `Callable[[str], None] \| None` | 錯誤回調 |

**`ProgressCallback` 簽名：**
```python
(msg: str, current: int, total: int, step: int, total_steps: int, eta_secs: int) -> None
```
- `msg`: 人類可讀的狀態訊息
- `current` / `total`: 當前進度（total = 0 表示不確定）
- `step`: 目前執行到第幾步（1–3）
- `total_steps`: 總步驟數（固定為 3）
- `eta_secs`: 預估剩餘秒數，-1 表示無法估算

**公開方法：**

| 方法 | 說明 |
|------|------|
| `scan()` | 執行掃描，呼叫 on_done/on_error，應在 worker thread 執行 |
| `stop()` | 設定 stop event（同時 set pause event 避免死鎖）|
| `pause()` | 清除 pause event，使掃描 thread 在下一個暫停點阻塞 |
| `resume()` | 設定 pause event，解除暫停 |
| `is_paused` | property，回傳當前是否暫停中 |

**私有方法：**

| 方法 | 說明 |
|------|------|
| `_check()` | 暫停點：等待 pause event，回傳 True 表示應停止（用於循序模式）|
| `_stopped()` | 非阻塞 stop 查詢（用於並行消費迴圈）|
| `_emit(...)` | 包裝 on_progress 呼叫 |
| `_eta(start, done, total)` | 依已用時間估算剩餘秒數 |
| `_pass2_sequential(candidates, total_c, t0)` | 循序 Pass 2（HDD 安全，細粒度暫停/停止）|
| `_pass2_parallel(candidates, total_c, t0, workers)` | 並行 Pass 2（ThreadPoolExecutor，SSD/NVMe）|
| `_pass3_sequential(candidates2, total2, t0)` | 循序 Pass 3 |
| `_pass3_parallel(candidates2, total2, t0, workers)` | 並行 Pass 3 |
| `_run()` | 執行 3-pass 演算法，依 workers 選擇循序或並行路徑 |

**並行模式執行緒安全說明：**

worker thread 只執行 `_partial_hash` / `_full_hash`（純函式，讀檔回傳字串）。
所有 dict/list 操作（`by_partial[ph].append(fi)`）在 scan thread 消費 `as_completed` 時循序執行，無 data race。
暫停以 `_pause_event.wait()` 在消費迴圈中阻塞；停止以 `_stopped()` 檢查並呼叫 `f.cancel()` 取消尚未開始的 futures。

---

### `src/logger.py`

| 名稱 | 說明 |
|------|------|
| `LOG_DIR` | `logs/` 目錄（相對於專案根目錄）|
| `logger` | 模組層級 Logger 單例，直接 `from src.logger import logger` 使用 |
| `logger.log_file` | 本次執行的日誌檔案路徑（字串）|

**日誌等級使用原則：**

| 等級 | 使用場景 |
|------|---------|
| `INFO` | 掃描開始/結束、Pass 完成、刪除操作 |
| `DEBUG` | 重複群組詳細資訊（hash、路徑等）|
| `WARNING` | 刪除失敗 |
| `ERROR` | 掃描引擎例外 |

---

### `src/ui/main_window.py`

#### `class ScanWorker(QObject)`

Qt 的 worker 包裝，`moveToThread` 到 `QThread` 執行。

**Signals：**

| Signal | 型別 | 說明 |
|--------|------|------|
| `progress` | `(str, int, int, int, int, int)` | 進度更新（透傳自 Scanner）|
| `finished` | `(list)` | 掃描完成，帶入 `list[DuplicateGroup]` |
| `error` | `(str)` | 發生錯誤 |
| `detected` | `(str)` | 磁碟偵測完成，帶入 `DiskProfile.summary()` 字串 |

**方法：**

| 方法 | 說明 |
|------|------|
| `run()` | 先呼叫 `detect_disk_profile`，emit `detected`，再建立並執行 Scanner |
| `stop()` | 轉發至 Scanner.stop() |
| `pause()` | 轉發至 Scanner.pause() |
| `resume()` | 轉發至 Scanner.resume() |

---

#### `class ImagePreviewPopup(QLabel)`

圖片懸停預覽浮動視窗（`Qt.WindowType.ToolTip | FramelessWindowHint`）。

| 方法 | 說明 |
|------|------|
| `show_image(path, global_pos)` | 載入圖片並縮放至 220×220（保持比例），移動到指定螢幕座標後顯示 |
| `reset()` | 清除快取路徑並隱藏視窗 |

快取機制：若 `path` 與上次相同且視窗已顯示，只移動位置不重新載入圖片（避免每次 MouseMove 都讀取磁碟）。

---

#### 模組層級：Condition 系統

| 名稱 | 說明 |
|------|------|
| `CONDITIONS_DEF` | 條件定義列表 `[(id, label, key_fn, reverse, conflict_id), ...]`，共 9 條 |
| `_COND_BY_ID` | `{id: condition_tuple}` 快速查找字典 |
| `_rank_by_conditions(files, conditions)` | 多條件穩定排序，回傳「勝出」檔案索引；`conditions = [(id, key_fn, reverse), ...]` |

條件 ID 對照表：

| ID | 標籤 | 排序邏輯 | 互斥 |
|----|------|---------|------|
| `newer` | 修改較新 | mtime 降序 | `older` |
| `older` | 修改較舊 | mtime 升序 | `newer` |
| `sh_path` | 路徑較短 | path 長度升序 | `lo_path` |
| `lo_path` | 路徑較長 | path 長度降序 | `sh_path` |
| `shallow` | 目錄較淺 | depth 升序 | `deep` |
| `deep` | 目錄較深 | depth 降序 | `shallow` |
| `al_first` | 字母較前 | path 字串升序 | `al_last` |
| `al_last` | 字母較後 | path 字串降序 | `al_first` |
| `list_1st` | 列表第一 | 原始索引升序 | 無 |

#### `class ConditionPanel(QWidget)`

可重用的多條件勾選面板。條件以互斥對排列（4對 + 1單項）。

| 方法 | 說明 |
|------|------|
| `get_active()` | 回傳 `[(id, key_fn, reverse), ...]`，依使用者勾選順序排列 |
| `clear_all()` | 清除所有勾選並重新啟用所有選項 |
| `_on_toggle(cond_id, state)` | 勾選時將衝突條件 disable 並移出優先清單；取消時恢復 |

---

#### `class GroupCard(QFrame)`

每個重複群組的 UI 卡片元件。

| 方法/屬性 | 說明 |
|-----------|------|
| `__init__(group, index, preview_popup, font_size=13)` | 建立卡片，渲染檔案清單 table；`font_size` 控制行高與表格字體（依比例縮放） |
| `_apply_conditions()` | 讀取卡片自身 ConditionPanel 的條件，呼叫 `_apply_with` |
| `_apply_with(conditions, mode)` | 呼叫 `_rank_by_conditions` 取得勝出索引，依 mode 設定 checkbox 狀態 |
| `checkboxes` | 每行的 QCheckBox 清單 |
| `selected_paths()` | 回傳所有被勾選的檔案路徑 |
| `_on_cell_clicked(row, col)` | `cellClicked` 接收者；col=1（檔名欄）時呼叫 `os.startfile` 開啟檔案 |
| `eventFilter(obj, event)` | 安裝在 `table.viewport()`；MouseMove 時若 col=1 且為圖片副檔名則呼叫 `preview_popup.show_image`；Leave 時呼叫 `reset()` |
| `_keep_newest()` | 快速選取：勾選除最新以外 |
| `_keep_oldest()` | 快速選取：勾選除最舊以外 |
| `_keep_first()` | 快速選取：勾選第 2 筆以後 |
| `_select_all()` | 全選 |
| `_deselect_all()` | 全不選 |

**檔名欄樣式**：藍色 (`ACCENT`) + 底線字型，點擊呼叫 `os.startfile` 以預設程式開啟。
**路徑欄樣式**：橘色 (`ACCENT3`) + 底線字型，點擊呼叫 `subprocess.Popen('explorer /select,"<path>"', shell=True)` 在 Windows 檔案總管中定位並選取檔案。

#### 選取策略一覽

| 方法 | 保留條件 |
|------|---------|
| `_keep_newest` | `mtime` 最大 |
| `_keep_oldest` | `mtime` 最小 |
| `_keep_shortest_path` | `len(str(path))` 最小 |
| `_keep_longest_path` | `len(str(path))` 最大 |
| `_keep_shallowest` | `len(path.parts)` 最小 |
| `_keep_deepest` | `len(path.parts)` 最大 |
| `_keep_alpha_first` | `str(path).lower()` 字母序最小 |
| `_keep_alpha_last` | `str(path).lower()` 字母序最大 |
| `_keep_first` | 列表索引 0 |

所有方法都透過 `_keep_index(i)` 實作，將除 `i` 以外的 checkbox 全勾選。

---

#### `class MainWindow(QMainWindow)`

**狀態欄位：**

| 欄位 | 說明 |
|------|------|
| `_scanning: bool` | 目前是否有掃描正在進行 |
| `_paused: bool` | 目前是否暫停中 |
| `_thread: QThread \| None` | 當前 worker thread |
| `_worker: ScanWorker \| None` | 當前 worker 物件 |
| `_scan_id: int` | 掃描流水號（每次 `_start_scan` 遞增），用於防止舊 thread signal 污染 |
| `_all_groups` | 最新一次掃描的完整結果（未篩選）|
| `_groups` | 目前顯示中的群組（套用篩選/排序後）|
| `_cards` | 對應的 GroupCard 元件清單 |
| `_preview_popup` | 共用的 `ImagePreviewPopup` 實例（所有 GroupCard 共享同一個）|
| `_font_size: int` | 目前套用的 UI 文字大小（預設 13px，範圍 9–18）|

**關鍵方法：**

| 方法 | 說明 |
|------|------|
| `_toggle_scan()` | 掃描按鈕的點擊處理：閒置時開始，掃描中時取消 |
| `_start_scan()` | 驗證路徑 → kill 舊 thread → 遞增 `_scan_id` → 建立 worker/thread → 開始掃描 |
| `_cancel_scan()` | 送 stop 訊號，等待 thread.finished 恢復 UI |
| `_toggle_pause()` | 暫停/繼續切換，更新按鈕外觀與 logger |
| `_reset_all()` | 強制 kill thread，清空所有 UI 狀態 |
| `_kill_thread()` | 同步等待 thread 結束（最多 5 秒），清除 worker/thread 參照 |
| `_set_scanning(bool)` | 切換掃描狀態，更新按鈕文字/顏色/可見性 |
| `_on_thread_finished(scan_id)` | `thread.finished` 接收者；比對 `scan_id` 與 `_scan_id`，若不符則忽略（防止 race condition） |
| `_on_progress(...)` | 更新步驟標籤、ETA、進度條 |
| `_on_done(groups)` | 儲存 `_all_groups`，顯示 filter_sort_sec，呼叫 `_apply_filter_sort` 渲染卡片；啟用 `btn_export` |
| `_on_error(msg)` | 顯示錯誤 dialog，記錄 log |
| `_apply_filter_sort()` | 依 ext_filter_edit 與 sort_combo 篩選/排序 `_all_groups`，呼叫 `_rebuild_cards` |
| `_rebuild_cards(groups)` | 清除舊 GroupCard，依傳入的 groups 重新建立（傳入 `_font_size`）|
| `_apply_font_size(size)` | 更新 `_font_size`，只重建卡片（不改全局 stylesheet），僅影響群組內表格字體與行高 |
| `_export_report()` | 彙整副檔名統計，呼叫 `_make_pie_svg` 產生兩張圓餅圖，輸出 HTML 報表並以瀏覽器開啟 |
| `_make_pie_svg(slices, title)` | 輸入 `[(label, value), ...]`，回傳含自動配色圓餅圖的 SVG 字串（右側附圖例）|
| `_toggle_sidebar()` | 切換側邊功能列顯示/隱藏，更新 ◀/▶ 圖示 |
| `_toggle_filter_panel()` | 切換側邊「篩選與排序」區段展開/收折 |
| `_toggle_qs_panel()` | 切換側邊「批量快速選取」區段展開/收折 |
| `_global_apply_conditions()` | 取得全局 ConditionPanel 的條件，套用到所有 GroupCard |
| `_set_global_mode(mode)` | 設定全局操作模式（保留/刪除），更新按鈕樣式 |
| `_global_apply(method_name)` | 對 `self._cards` 中每個 GroupCard 呼叫指定選取方法 |
| `_global_keep_newest()` … | 9 種全域快速選取，各自委派給 `_global_apply` |
| `_global_select_all/deselect_all()` | 全域全選 / 全不選 |
| `_delete_selected()` | 確認刪除 → `Path.unlink()` → 記錄 log → 重新掃描 |

---

## 掃描演算法

### 3-Pass 策略

```
所有檔案 (N 個)
    │
    ▼ Pass 1：os.walk 遍歷，依 size 分組
    │  dict[int, list[FileInfo]]
    │  ── 刪除 size 唯一的 key（無重複可能）
    │  剩餘：M 個候選（M << N）
    │
    ▼ Pass 2：讀取前 PARTIAL_READ (4KB) 計算 hash
    │  dict[str, list[FileInfo]]
    │  ── 刪除 hash 唯一的 key
    │  剩餘：K 個候選（K << M，通常過濾 90%+）
    │
    ▼ Pass 3：讀取整個檔案計算 xxHash
    │  dict[str, list[FileInfo]]
    │  ── 只保留 hash 重複的 key
    │
    └─→ 建立 list[DuplicateGroup]，依 wasted_bytes 排序
```

### 複雜度分析

| Pass | 磁碟 I/O | 說明 |
|------|---------|------|
| Pass 1 | 0（只讀 stat）| 純記憶體操作，O(N) |
| Pass 2 | 每個候選讀 4KB | 成本低，可過濾大量偽候選 |
| Pass 3 | 只對剩餘候選全讀 | 最耗時，但通常檔案數極少 |

---

## Threading 模型

```
Main Thread (Qt Event Loop)
    │
    │  QThread.start()
    ▼
Worker Thread
    └─ ScanWorker.run()
        └─ Scanner._run()  ← 實際執行 os.walk / hash

Thread 通訊（thread-safe）：
  Worker → Main：Qt signals (progress / finished / error)
  Main → Worker：threading.Event (stop_event / pause_event)
```

**暫停機制：**

```python
# pause_event: threading.Event
# set()   → 繼續（事件已設定，wait() 立即返回）
# clear() → 暫停（wait() 阻塞直到 set()）

def _check(self) -> bool:
    self._pause_event.wait()   # 若已 clear()，在此阻塞
    return self._stop_event.is_set()
```

> **重要**：`stop()` 必須同時呼叫 `_pause_event.set()`，否則暫停中的 thread 永遠無法收到 stop 訊號，導致死鎖。

---

## 狀態機設計

```
                    ┌─────────────────────────────────────────┐
                    ▼                                         │
IDLE ──[開始掃描]──→ SCANNING ──[取消]──→ CANCELING ──[thread.finished]──┘
                       │  ▲
                   [暫停]  [繼續]
                       │  │
                       ▼  │
                     PAUSED

任何狀態 ──[重置]──→ IDLE（強制 _kill_thread）
```

**按鈕可見性對照：**

| 狀態 | btn_scan | btn_pause | btn_reset |
|------|---------|-----------|-----------|
| IDLE | ▶ 開始掃描 | 隱藏 | 顯示 |
| SCANNING | ✕ 取消掃描 | ⏸ 暫停 | 顯示 |
| PAUSED | ✕ 取消掃描 | ▶ 繼續 | 顯示 |
| CANCELING | 取消中...（disabled）| 隱藏 | 顯示 |

---

## UI 分批渲染

### 問題描述

掃描大量檔案（例如整顆磁碟）完成後，`_on_done()` 呼叫 `_apply_filter_sort()` → `_rebuild_cards()`，一次性在主執行緒建立數百個 `GroupCard`（每個含 `QTableWidget`），造成主執行緒長時間阻塞，側邊功能列（篩選、排序、批量選取）反白無回應。

### 解法

引入 `_render_pending: list` 與 `_render_generation: int`，並用 `QTimer.singleShot(0, ...)` 將卡片建立分散到多個事件迴圈 tick：

```python
_RENDER_BATCH = 8  # 每批建立的卡片數量

def _rebuild_cards(self, groups):
    self._render_generation += 1        # 使舊的 in-flight batch 失效
    self._render_pending = list(enumerate(groups))
    self._schedule_batch(self._render_generation)

def _schedule_batch(self, gen):
    if gen != self._render_generation:  # 已被新的 rebuild 取代，放棄
        return
    for _ in range(self._RENDER_BATCH):
        if not self._render_pending:
            return
        i, group = self._render_pending.pop(0)
        card = GroupCard(...)
        self.results_layout.insertWidget(i, card)
    if self._render_pending:
        QTimer.singleShot(0, lambda g=gen: self._schedule_batch(g))
```

`QTimer.singleShot(0, ...)` 將下一批安排在事件佇列末端，使 Qt 能在批次之間處理滑鼠、鍵盤、繪製等事件，側邊列保持可互動。`_render_generation` 計數器確保當篩選條件變更觸發新的 `_rebuild_cards` 時，舊的批次回呼會直接返回，不會與新批次交錯。

---

## 日誌系統

**日誌位置：** `logs/dupescan_YYYYMMDD_HHMMSS.log`

每次程式啟動產生一個新的 log 檔案，不覆蓋舊的。`logs/` 目錄已加入 `.gitignore`。

**典型日誌範例：**

```
2026-04-01 09:30:00 [INFO ] 掃描開始 — 路徑: D:\Photos, 最小大小: 1024 B
2026-04-01 09:30:02 [INFO ] 掃描完成 — 3 個重複群組，共浪費 12.4 MiB（13,000,000 B）
2026-04-01 09:30:02 [DEBUG] 群組 hash=a1b2c3d4e5f6ff01  size=4.2 MiB  files=2  wasted=4.2 MiB
2026-04-01 09:30:02 [DEBUG] 群組 hash=beef1234abcd5678  size=2.1 MiB  files=3  wasted=4.2 MiB
2026-04-01 09:30:15 [INFO ] 開始刪除 2 個檔案
2026-04-01 09:30:15 [INFO ]   刪除: D:\Photos\backup\vacation.jpg
2026-04-01 09:30:15 [INFO ] 刪除完成 — 成功 2 個，失敗 0 個
```

---

## 效能考量

1. **xxHash 優先**：比 MD5 快 ~10x、比 SHA256 快 ~20x。若未安裝自動退回 `hashlib.blake2b`。
2. **3-Pass 設計**：大幅減少全讀次數。在典型情況下（大量小差異檔案），Pass 3 可能只需處理個位數檔案。
3. **os.walk 不跟隨 symlink**（`followlinks=False`）：避免循環引用造成無限遍歷。
4. **時間節流 Progress Signal**：Pass 2 / Pass 3 使用 `time.monotonic()` 節流，每 `EMIT_INTERVAL`（0.15 秒）最多 emit 一次。掃描數萬個候選時，避免 Qt signal 佇列積壓造成 UI 卡頓（舊做法是每 50 個 / 每個檔案 emit，百萬量級時過於頻繁）。
5. **UI 分批渲染**：見 [UI 分批渲染](#ui-分批渲染)章節。GroupCard 以 `_RENDER_BATCH = 8` 每批建立，批次間讓出 Qt 事件迴圈，防止 UI 反白。
6. **Pass 3 I/O 吞吐**：`CHUNK_SIZE` 從 64 KB 提升至 128 KB，減少 `read()` 系統呼叫次數，提升大型檔案的 Hash 速度。
7. **自適應多執行緒**：掃描前由 `disk_detect` 偵測磁碟類型，SSD/NVMe 啟用 `ThreadPoolExecutor` 並行 I/O；HDD 維持單執行緒順序讀取，避免隨機尋軌損耗。Python GIL 在 file I/O 與 xxhash（C 擴充）期間自動釋放，多執行緒效益可實際發揮。
8. **UI 渲染**：GroupCard 使用固定高度 QTableWidget，避免動態計算帶來的效能問題。

---

## 關鍵常數

| 常數 | 位置 | 值 | 說明 |
|------|------|----|------|
| `PARTIAL_READ` | scanner.py | 4096 | Pass 2 讀取 bytes 數 |
| `CHUNK_SIZE` | scanner.py | 131072 | Pass 3 chunk 大小（128 KB）|
| `EMIT_INTERVAL` | scanner.py | 0.15 | Pass 2/3 進度 signal 最小間隔（秒）|
| `BUS_NVME` | disk_detect.py | 17 | STORAGE_BUS_TYPE NVMe 枚舉值（ntddstor.h）|
| `BUS_SATA` | disk_detect.py | 11 | STORAGE_BUS_TYPE SATA 枚舉值 |
| `TOTAL_STEPS` | scanner.py | 3 | 掃描步驟總數 |
| `_RENDER_BATCH` | main_window.py | 8 | 每批建立的 GroupCard 數量 |
| `_kill_thread` timeout | main_window.py | 5000 ms | 強制等待 thread 結束的上限 |
| `IMAGE_EXTENSIONS` | main_window.py | set of str | 支援懸停預覽的圖片副檔名集合 |
| `DARK_BG` / `ACCENT` etc. | main_window.py | hex colors | 深色主題配色，集中在檔案頂部 |

---

## 掃描 Race Condition 修正

### 問題描述

舊版本中，`_on_thread_finished` 無條件將 `self._thread` 與 `self._worker` 設為 `None`。
當掃描結束後使用者立即點擊「開始掃描」，`_start_scan` 會建立新的 thread/worker 並開始運行；
但此時舊 thread 尚未發送 `finished` signal（signal 排入 Qt event queue），等排到時便覆蓋了新掃描的參照，導致閃退或狀態異常。

### 解法

引入 `_scan_id: int`（每次 `_start_scan` 遞增），並在連接 `thread.finished` 時以 lambda 捕捉當時的 `_scan_id`：

```python
self._scan_id += 1
current_scan_id = self._scan_id
self._thread.finished.connect(
    lambda sid=current_scan_id: self._on_thread_finished(sid)
)
```

`_on_thread_finished` 收到 signal 後，先比對 `scan_id == self._scan_id`；
若不符（表示這是舊 thread 的 stale signal）則直接返回，不修改任何狀態。
