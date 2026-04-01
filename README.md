# SimulateInput

macOS menu bar app，隨機模擬按鍵防止螢幕鎖定，偵測到真實輸入後自動暫停，idle 後自動恢復。

## 功能

- **防鎖屏**：每隔隨機間隔模擬一次安全按鍵（Shift / Ctrl / Alt / F15）
- **自動暫停**：偵測到鍵盤、滑鼠點擊或滾輪後暫停模擬
- **自動恢復**：超過設定的 idle 時間後恢復模擬
- **開機自動啟動**：透過 SMAppService 整合系統登入項目（macOS 13+）
- **Menu Bar 常駐**：不占用 Dock，僅顯示於右上角狀態列

## 狀態圖示

| 圖示 | 狀態 |
|------|------|
| ▶ （三角形）| 模擬執行中 |
| ⏸ （雙豎條）| 偵測到輸入，已暫停 |
| ⏹ （實心圓）| 已停止 |

## Menu 選項

- **啟動 / 停止**：手動切換模擬狀態
- **開機自動啟動**：勾選後下次登入自動啟動
- **調整參數**
  - Idle Timeout：真實輸入停止多久後恢復模擬（預設 30s）
  - 最短間隔：兩次模擬按鍵的最短間隔（預設 5s）
  - 最長間隔：兩次模擬按鍵的最長間隔（預設 15s）
- **查看 Log**：顯示最近 20 筆操作紀錄
- **結束**：停止模擬並退出 app

## 技術細節

- 按鍵模擬使用 `Quartz.CGEventCreateKeyboardEvent`（hardcoded VK codes，不呼叫 TIS API）
  → 相容 macOS 15 對 `TSMGetInputSourceProperty` 的 main thread 限制
- 輸入監聽使用 `AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_`
- UI 更新採 polling 架構：background thread 寫入 `_pending_ui`，`rumps.Timer` 每 0.5s 在 main thread 同步

## 環境需求

- macOS 13+
- Python 3.12+
- 系統設定 → 隱私與安全性 → 輔助使用：授權 app

## 開發 & 打包

### 安裝 uv

uv 是獨立工具，需另行安裝（非 Python 內建）：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或用 Homebrew
brew install uv
```

### 建置流程

```bash
# 安裝依賴
uv sync

# 直接執行（開發用）
uv run python app_main.py

# 打包為 .app
uv run python setup.py py2app

# 安裝
cp -r dist/SimulateInput.app /Applications/
```
