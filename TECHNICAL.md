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
├── requirements.txt     # pip 依賴清單
├── .gitignore
├── README.md            # 使用者操作說明
├── TECHNICAL.md         # 本檔案（技術手冊）
├── logs/                # 執行日誌，每次啟動產生新檔案（gitignore）
└── src/
    ├── __init__.py
    ├── models.py        # 資料結構定義
    ├── scanner.py       # 掃描引擎（核心演算法）
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
| `_check()` | 暫停點：等待 pause event，回傳 True 表示應停止 |
| `_emit(...)` | 包裝 on_progress 呼叫 |
| `_eta(start, done, total)` | 依已用時間估算剩餘秒數 |
| `_run()` | 執行 3-pass 演算法，回傳 `list[DuplicateGroup]` |

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

**方法：**

| 方法 | 說明 |
|------|------|
| `run()` | 在 worker thread 中執行掃描 |
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

#### `class GroupCard(QFrame)`

每個重複群組的 UI 卡片元件。

| 方法/屬性 | 說明 |
|-----------|------|
| `__init__(group, index, preview_popup)` | 建立卡片，渲染檔案清單 table；需傳入共用的 `ImagePreviewPopup` 實例 |
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
| `_on_done(groups)` | 儲存 `_all_groups`，顯示 filter_sort_box，呼叫 `_apply_filter_sort` 渲染卡片 |
| `_on_error(msg)` | 顯示錯誤 dialog，記錄 log |
| `_apply_filter_sort()` | 依 ext_filter_edit 與 sort_combo 篩選/排序 `_all_groups`，呼叫 `_rebuild_cards` |
| `_rebuild_cards(groups)` | 清除舊 GroupCard，依傳入的 groups 重新建立 |
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
4. **暫停點間距**：每處理 50~300 個檔案才 emit 一次 progress signal，避免 Qt 事件佇列塞滿。
5. **UI 渲染**：GroupCard 使用固定高度 QTableWidget，避免動態計算帶來的效能問題。

---

## 關鍵常數

| 常數 | 位置 | 值 | 說明 |
|------|------|----|------|
| `PARTIAL_READ` | scanner.py | 4096 | Pass 2 讀取 bytes 數 |
| `CHUNK_SIZE` | scanner.py | 65536 | Pass 3 chunk 大小 |
| `TOTAL_STEPS` | scanner.py | 3 | 掃描步驟總數 |
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
