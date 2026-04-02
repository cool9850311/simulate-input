"""
SimulateInput — macOS Menu Bar App
隨機模擬按鍵防鎖屏，偵測到真實輸入自動暫停，idle 後恢復。

關鍵設計：
- 按鍵模擬：直接用 Quartz CGEventCreateKeyboardEvent（不用 pynput Controller）
  → 避免 pynput Controller 在 background thread 呼叫 TISCopyCurrentKeyboardInputSource
     （macOS 15 起 TSMGetInputSourceProperty 只能在 main thread 呼叫）
- 輸入監聽：AppKit NSEvent.addGlobalMonitorForEventsMatchingMask（原生，無 TIS 問題）
- UI 更新：polling rumps.Timer（主執行緒），simulation thread 只寫 state dict
"""

import json
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime

import AppKit
import rumps
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGEventMouseMoved,
    kCGHIDEventTap,
)


# ── 按鍵虛擬碼（固定值，不需 TIS 查詢）──────────────────────────────────────
_SAFE_VKS = [
    0x38,  # Left Shift
    0x3B,  # Left Control
    0x3A,  # Left Option/Alt
    0x71,  # F15
]

# ── 預設參數 ──────────────────────────────────────────────────────────────────
DEFAULT_IDLE_TIMEOUT = 180
DEFAULT_INTERVAL_MIN = 5
DEFAULT_INTERVAL_MAX = 15
DEFAULT_SCHEDULE_START = "09:00"
DEFAULT_SCHEDULE_STOP = "18:00"
DEFAULT_SCHEDULE_WEEKDAYS = "weekdays"  # "all" | "weekdays" | "weekend" | "custom:0,1,2,3,4"

if getattr(sys, "frozen", False):
    _resources = os.path.join(os.path.dirname(sys.executable), "..", "Resources")
else:
    _resources = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(_resources, "icons")
LOG_MAX = 20
_config_dir = os.path.expanduser("~/Library/Application Support/SimulateInput")
os.makedirs(_config_dir, exist_ok=True)
CONFIG_PATH = os.path.join(_config_dir, "config.json")
# ─────────────────────────────────────────────────────────────────────────────

_TITLE_FALLBACK = {"running": "▶", "paused": "⏸", "stopped": "⏹"}
_VK_NAMES = {0x38: "Shift", 0x3B: "Ctrl", 0x3A: "Alt", 0x71: "F15"}


def icon_path(name: str) -> str:
    p = os.path.join(ICON_DIR, f"{name}.png")
    return p if os.path.exists(p) else None


def simulate_key(vk: int):
    """直接用 CGEvent 按下並釋放按鍵，不呼叫任何 TIS API。"""
    down = CGEventCreateKeyboardEvent(None, vk, True)
    up   = CGEventCreateKeyboardEvent(None, vk, False)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def simulate_activity_burst(stop_event: threading.Event, duration: float = 6.0, step_interval: float = 0.2):
    """持續微移滑鼠 duration 秒，若 stop_event 被清除則立即中斷。"""
    loc = AppKit.NSEvent.mouseLocation()
    x, y = loc.x, loc.y
    steps = int(duration / step_interval)
    for i in range(steps):
        if not stop_event.is_set():
            break
        offset = 50 if i % 2 == 0 else -50
        e = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x + offset, y), 0)
        CGEventPost(kCGHIDEventTap, e)
        time.sleep(step_interval)


class SimulateInputApp(rumps.App):
    def __init__(self):
        _icon = icon_path("stopped")
        super().__init__(
            name="SimulateInput",
            icon=_icon,
            title=None if _icon else _TITLE_FALLBACK["stopped"],
            quit_button=None,
        )

        # ── 執行狀態 ──────────────────────────────────────────────────────────
        self._running = False
        self._resume_event = threading.Event()
        self._idle_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._sim_thread: threading.Thread | None = None
        self._event_monitor = None

        # ── UI 狀態（simulation thread 只寫這裡；polling timer 讀並更新 UI）──
        self._pending_ui: dict | None = None
        self._ui_lock = threading.Lock()

        # ── Log ───────────────────────────────────────────────────────────────
        self._log: deque = deque(maxlen=LOG_MAX)
        self._log_lock = threading.Lock()

        # ── 參數 ──────────────────────────────────────────────────────────────
        cfg = self._load_config()
        self.idle_timeout = cfg.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)
        self.interval_min = cfg.get("interval_min", DEFAULT_INTERVAL_MIN)
        self.interval_max = cfg.get("interval_max", DEFAULT_INTERVAL_MAX)
        self.schedule_enabled = cfg.get("schedule_enabled", False)
        self.schedule_start = cfg.get("schedule_start", DEFAULT_SCHEDULE_START)
        self.schedule_stop = cfg.get("schedule_stop", DEFAULT_SCHEDULE_STOP)
        self.schedule_weekdays = cfg.get("schedule_weekdays", DEFAULT_SCHEDULE_WEEKDAYS)

        # ── Menu 項目 ─────────────────────────────────────────────────────────
        self._status_item = rumps.MenuItem("狀態：已停止")
        self._status_item.set_callback(None)

        self._toggle_item = rumps.MenuItem("▶  啟動", callback=self.toggle)

        self._login_item = rumps.MenuItem("開機自動啟動", callback=self.toggle_login)
        self._login_item.state = self._get_login_state()

        self._schedule_item = rumps.MenuItem("定時啟動/關閉", callback=self.toggle_schedule)
        self._schedule_item.state = 1 if self.schedule_enabled else 0

        self._schedule_start_item = rumps.MenuItem(
            f"啟動時間：{self.schedule_start}", callback=self.set_schedule_start)
        self._schedule_stop_item = rumps.MenuItem(
            f"關閉時間：{self.schedule_stop}", callback=self.set_schedule_stop)
        self._schedule_weekdays_item = rumps.MenuItem(
            f"執行日期：{self._get_weekdays_label()}", callback=self.set_schedule_weekdays)
        schedule_config_menu = rumps.MenuItem("設定定時啟動")
        schedule_config_menu.update([
            self._schedule_start_item,
            self._schedule_stop_item,
            self._schedule_weekdays_item
        ])

        self._param_idle = rumps.MenuItem(
            f"Idle Timeout：{self.idle_timeout}s", callback=self.set_idle_timeout)
        self._param_min = rumps.MenuItem(
            f"最短間隔：{self.interval_min}s", callback=self.set_interval_min)
        self._param_max = rumps.MenuItem(
            f"最長間隔：{self.interval_max}s", callback=self.set_interval_max)
        params_menu = rumps.MenuItem("調整參數")
        params_menu.update([self._param_idle, self._param_min, self._param_max])

        self._perm_accessibility = rumps.MenuItem("輔助使用設定…", callback=self._open_accessibility)
        self._perm_input         = rumps.MenuItem("輸入監控設定…", callback=self._open_input_monitoring)

        self._log_item  = rumps.MenuItem("查看 Log", callback=self.show_log)
        self._quit_item = rumps.MenuItem("結束",     callback=self.quit_app)

        self.menu = [
            self._status_item, None,
            self._toggle_item, None,
            self._login_item,  None,
            self._schedule_item,
            schedule_config_menu,
            None,
            params_menu,
            None,
            self._perm_accessibility,
            self._perm_input,
            None,
            self._log_item,    None,
            self._quit_item,
        ]

        # ── 每 0.5 秒在主執行緒同步 UI（避免 cross-thread UI 更新）───────────
        self._ui_sync_timer = rumps.Timer(self._sync_ui, 0.5)
        self._ui_sync_timer.start()

        # ── 每 60 秒檢查定時啟動/關閉 ────────────────────────────────────────
        self._schedule_timer = rumps.Timer(self._check_schedule, 60)
        self._schedule_timer.start()

        # ── 啟動後自動開始模擬（延遲 0.3s 確保 run loop 已就緒）────────────
        self._boot_timer = rumps.Timer(self._auto_start, 0.3)
        self._boot_timer.start()

    def _open_accessibility(self, _):
        url = AppKit.NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )
        AppKit.NSWorkspace.sharedWorkspace().openURL_(url)

    def _open_input_monitoring(self, _):
        url = AppKit.NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        )
        AppKit.NSWorkspace.sharedWorkspace().openURL_(url)

    def _auto_start(self, timer):
        timer.stop()
        self._start()

    # ── UI 同步（主執行緒 polling）────────────────────────────────────────────
    def _sync_ui(self, _):
        with self._ui_lock:
            pending = self._pending_ui
            self._pending_ui = None
        if pending is None:
            return
        icon_name   = pending["icon"]
        status      = pending["status"]
        toggle_title = pending["toggle"]
        p = icon_path(icon_name)
        if p:
            self.icon  = p
            self.title = None
        else:
            self.title = _TITLE_FALLBACK[icon_name]
        self._status_item.title  = status
        self._toggle_item.title  = toggle_title

    def _request_ui(self, icon_name: str, status: str, toggle_title: str):
        """任意執行緒都可呼叫；UI 更新會由 polling timer 在主執行緒完成。"""
        with self._ui_lock:
            self._pending_ui = {
                "icon": icon_name, "status": status, "toggle": toggle_title
            }

    def _add_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self._log_lock:
            self._log.append(f"[{ts}] {msg}")

    # ── Toggle 啟動/停止 ───────────────────────────────────────────────────────
    def toggle(self, _):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        self._running = True
        self._resume_event.set()
        self._add_log("▶ 啟動模擬")
        self._request_ui("running", "狀態：執行中", "⏹  停止")

        self._sim_thread = threading.Thread(target=self._simulation_loop, daemon=True)
        self._sim_thread.start()

        mask = (
            AppKit.NSEventMaskKeyDown
            | AppKit.NSEventMaskLeftMouseDown
            | AppKit.NSEventMaskRightMouseDown
            | AppKit.NSEventMaskScrollWheel
        )
        self._event_monitor = \
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, lambda _e: self._on_user_input()
            )

    def _stop(self):
        self._running = False
        self._resume_event.clear()
        with self._timer_lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
        if self._event_monitor:
            AppKit.NSEvent.removeMonitor_(self._event_monitor)
            self._event_monitor = None
        self._add_log("⏹ 停止模擬")
        self._request_ui("stopped", "狀態：已停止", "▶  啟動")

    # ── 模擬迴圈（background thread，完全不碰 TIS）───────────────────────────
    def _simulation_loop(self):
        while self._running:
            self._resume_event.wait()
            if not self._running:
                break
            interval = random.uniform(self.interval_min, self.interval_max)
            elapsed = 0.0
            while elapsed < interval and self._running:
                if not self._resume_event.is_set():
                    break
                time.sleep(1.0)
                elapsed += 1.0
            if not self._running or not self._resume_event.is_set():
                continue
            vk = random.choice(_SAFE_VKS)
            simulate_key(vk)
            simulate_activity_burst(self._resume_event)
            self._add_log(f"按鍵：{_VK_NAMES[vk]} + 連續滑鼠活動 6s（下次 {interval:.0f}s 後）")

    # ── 使用者輸入回呼（主執行緒）─────────────────────────────────────────────
    def _on_user_input(self):
        if not self._running:
            return
        with self._timer_lock:
            if self._resume_event.is_set():
                self._resume_event.clear()
                self._add_log("⏸ 偵測到輸入，暫停模擬")
                self._request_ui("paused", "狀態：已暫停", "⏹  停止")
            if self._idle_timer:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(self.idle_timeout, self._on_resume)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _on_resume(self):
        if self._running:
            self._resume_event.set()
            self._add_log(f"▶ 無輸入 {self.idle_timeout}s，恢復模擬")
            self._request_ui("running", "狀態：執行中", "⏹  停止")

    # ── 定時啟動/關閉 ───────────────────────────────────────────────────────────
    def _check_schedule(self, _):
        """每分鐘檢查是否需要自動啟動或關閉。"""
        if not self.schedule_enabled:
            return

        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_weekday = now.weekday()  # 0=週一, 6=週日

        # 檢查今天是否應該執行
        if not self._should_run_today(current_weekday):
            return

        # 檢查是否到達啟動時間
        if current_time == self.schedule_start:
            if not self._running:
                self._add_log(f"⏰ 定時啟動 {self.schedule_start}")
                self._start()

        # 檢查是否到達關閉時間
        elif current_time == self.schedule_stop:
            if self._running:
                self._add_log(f"⏰ 定時關閉 {self.schedule_stop}")
                self._stop()

    def _should_run_today(self, weekday: int) -> bool:
        """檢查今天是否應該執行定時任務。weekday: 0=週一, 6=週日"""
        if self.schedule_weekdays == "all":
            return True
        elif self.schedule_weekdays == "weekdays":
            return weekday < 5  # 0-4 是週一到週五
        elif self.schedule_weekdays == "weekend":
            return weekday >= 5  # 5-6 是週六週日
        elif self.schedule_weekdays.startswith("custom:"):
            days = self.schedule_weekdays.split(":")[1].split(",")
            return str(weekday) in days
        return True

    def _get_weekdays_label(self) -> str:
        """取得執行日期的顯示標籤。"""
        if self.schedule_weekdays == "all":
            return "每天"
        elif self.schedule_weekdays == "weekdays":
            return "僅工作日"
        elif self.schedule_weekdays == "weekend":
            return "僅週末"
        elif self.schedule_weekdays.startswith("custom:"):
            days = self.schedule_weekdays.split(":")[1].split(",")
            day_names = ["一", "二", "三", "四", "五", "六", "日"]
            selected = [f"週{day_names[int(d)]}" for d in days]
            return "自訂（" + "、".join(selected) + "）"
        return "每天"

    # ── 設定持久化 ──────────────────────────────────────────────────────────────
    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump({
                    "idle_timeout": self.idle_timeout,
                    "interval_min": self.interval_min,
                    "interval_max": self.interval_max,
                    "schedule_enabled": self.schedule_enabled,
                    "schedule_start": self.schedule_start,
                    "schedule_stop": self.schedule_stop,
                    "schedule_weekdays": self.schedule_weekdays,
                }, f)
        except Exception:
            pass

    # ── 開機自動啟動 ────────────────────────────────────────────────────────────
    def toggle_login(self, sender):
        try:
            from ServiceManagement import SMAppService
            svc = SMAppService.mainAppService()
            if sender.state:
                svc.unregisterAndReturnError_(None)
                sender.state = False
                self._add_log("取消開機啟動")
            else:
                ok, err = svc.registerAndReturnError_(None)
                if ok:
                    sender.state = True
                    self._add_log("已設定開機啟動")
                else:
                    rumps.alert("開機啟動", f"設定失敗：{err}")
        except Exception as e:
            rumps.alert("開機啟動", f"不支援（需 macOS 13+）\n{e}")

    def _get_login_state(self) -> int:
        try:
            from ServiceManagement import SMAppService
            svc = SMAppService.mainAppService()
            return 1 if int(svc.status()) == 1 else 0  # 1 = SMAppServiceStatusEnabled
        except Exception:
            return 0

    # ── 調整參數 ────────────────────────────────────────────────────────────────
    def set_idle_timeout(self, _):
        w = rumps.Window(message="輸入 Idle Timeout（秒）：", title="調整參數",
                         default_text=str(self.idle_timeout),
                         ok="確認", cancel="取消", dimensions=(200, 24))
        r = w.run()
        if r.clicked and r.text.strip().isdigit():
            self.idle_timeout = int(r.text.strip())
            self._param_idle.title = f"Idle Timeout：{self.idle_timeout}s"
            self._add_log(f"Idle Timeout 改為 {self.idle_timeout}s")
            self._save_config()

    def set_interval_min(self, _):
        w = rumps.Window(message="輸入最短間隔（秒）：", title="調整參數",
                         default_text=str(self.interval_min),
                         ok="確認", cancel="取消", dimensions=(200, 24))
        r = w.run()
        if r.clicked and r.text.strip().isdigit():
            v = int(r.text.strip())
            if v < self.interval_max:
                self.interval_min = v
                self._param_min.title = f"最短間隔：{self.interval_min}s"
                self._add_log(f"最短間隔改為 {self.interval_min}s")
                self._save_config()
            else:
                rumps.alert("錯誤", "最短間隔必須小於最長間隔")

    def set_interval_max(self, _):
        w = rumps.Window(message="輸入最長間隔（秒）：", title="調整參數",
                         default_text=str(self.interval_max),
                         ok="確認", cancel="取消", dimensions=(200, 24))
        r = w.run()
        if r.clicked and r.text.strip().isdigit():
            v = int(r.text.strip())
            if v > self.interval_min:
                self.interval_max = v
                self._param_max.title = f"最長間隔：{self.interval_max}s"
                self._add_log(f"最長間隔改為 {self.interval_max}s")
                self._save_config()
            else:
                rumps.alert("錯誤", "最長間隔必須大於最短間隔")

    # ── 定時功能設定 ────────────────────────────────────────────────────────────
    def toggle_schedule(self, sender):
        """切換定時啟動/關閉功能。"""
        self.schedule_enabled = not sender.state
        sender.state = 1 if self.schedule_enabled else 0
        status = "啟用" if self.schedule_enabled else "停用"
        self._add_log(f"定時功能已{status}")
        self._save_config()

    def set_schedule_start(self, _):
        """設定啟動時間。"""
        w = rumps.Window(
            message="輸入啟動時間（格式 HH:MM，例如 09:00）：",
            title="設定啟動時間",
            default_text=self.schedule_start,
            ok="確認", cancel="取消",
            dimensions=(200, 24)
        )
        r = w.run()
        if r.clicked and self._validate_time(r.text.strip()):
            self.schedule_start = r.text.strip()
            self._schedule_start_item.title = f"啟動時間：{self.schedule_start}"
            self._add_log(f"啟動時間設為 {self.schedule_start}")
            self._save_config()
        elif r.clicked:
            rumps.alert("錯誤", "時間格式錯誤，請使用 HH:MM 格式（例如 09:00）")

    def set_schedule_stop(self, _):
        """設定關閉時間。"""
        w = rumps.Window(
            message="輸入關閉時間（格式 HH:MM，例如 18:00）：",
            title="設定關閉時間",
            default_text=self.schedule_stop,
            ok="確認", cancel="取消",
            dimensions=(200, 24)
        )
        r = w.run()
        if r.clicked and self._validate_time(r.text.strip()):
            self.schedule_stop = r.text.strip()
            self._schedule_stop_item.title = f"關閉時間：{self.schedule_stop}"
            self._add_log(f"關閉時間設為 {self.schedule_stop}")
            self._save_config()
        elif r.clicked:
            rumps.alert("錯誤", "時間格式錯誤，請使用 HH:MM 格式（例如 18:00）")

    def set_schedule_weekdays(self, _):
        """設定執行日期。"""
        w = rumps.Window(
            message="選擇執行日期：\n1. 每天\n2. 僅工作日（週一～週五）\n3. 僅週末（週六～週日）\n4. 自訂（輸入數字，例如：0,1,2,3,4 表示週一到週五）",
            title="設定執行日期",
            default_text="1",
            ok="確認", cancel="取消",
            dimensions=(400, 24)
        )
        r = w.run()
        if r.clicked:
            choice = r.text.strip()
            if choice == "1":
                self.schedule_weekdays = "all"
            elif choice == "2":
                self.schedule_weekdays = "weekdays"
            elif choice == "3":
                self.schedule_weekdays = "weekend"
            elif choice == "4":
                # 再開一個對話框讓用戶輸入自訂日期
                w2 = rumps.Window(
                    message="輸入要執行的日期（0=週一, 6=週日）\n例如：0,1,2,3,4 表示週一到週五",
                    title="自訂執行日期",
                    default_text="0,1,2,3,4",
                    ok="確認", cancel="取消",
                    dimensions=(300, 24)
                )
                r2 = w2.run()
                if r2.clicked and self._validate_weekdays(r2.text.strip()):
                    self.schedule_weekdays = f"custom:{r2.text.strip()}"
                elif r2.clicked:
                    rumps.alert("錯誤", "格式錯誤，請輸入 0-6 的數字，以逗號分隔")
                    return
            else:
                rumps.alert("錯誤", "請輸入 1、2、3 或 4")
                return

            self._schedule_weekdays_item.title = f"執行日期：{self._get_weekdays_label()}"
            self._add_log(f"執行日期設為：{self._get_weekdays_label()}")
            self._save_config()

    def _validate_time(self, time_str: str) -> bool:
        """驗證時間格式是否正確（HH:MM）。"""
        try:
            datetime.strptime(time_str, "%H:%M")
            return True
        except ValueError:
            return False

    def _validate_weekdays(self, weekdays_str: str) -> bool:
        """驗證星期設定格式是否正確。"""
        try:
            days = [int(d.strip()) for d in weekdays_str.split(",")]
            return all(0 <= d <= 6 for d in days)
        except Exception:
            return False

    # ── Log ─────────────────────────────────────────────────────────────────────
    def show_log(self, _):
        with self._log_lock:
            content = "\n".join(self._log) if self._log else "（尚無紀錄）"
        rumps.alert(title="Log（最近 20 筆）", message=content)

    # ── 結束 ─────────────────────────────────────────────────────────────────────
    def quit_app(self, _):
        if self._running:
            self._stop()
        rumps.quit_application()


if __name__ == "__main__":
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )
    SimulateInputApp().run()
