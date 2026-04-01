"""
py2app 打包設定
用法：
    uv run python setup.py py2app
產出：dist/SimulateInput.app
"""
import py2app.build_app as _b

# py2app 0.28 不接受 install_requires（改從 venv 讀取依賴）
# 但 setuptools 會把 pyproject.toml [project].dependencies 塞入 distribution.install_requires
# patch finalize_options，在檢查前清空它。
_orig_finalize = _b.py2app.finalize_options
def _patched_finalize(self):
    self.distribution.install_requires = []
    _orig_finalize(self)
_b.py2app.finalize_options = _patched_finalize

from setuptools import setup

APP = ["app_main.py"]
DATA_FILES = [("icons", ["icons/running.png", "icons/paused.png", "icons/stopped.png"])]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "SimulateInput",
        "CFBundleDisplayName": "SimulateInput",
        "CFBundleIdentifier": "com.user.simulate-input",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,
        "NSAccessibilityUsageDescription": "SimulateInput 需要輔助使用權限以監聽鍵盤/滑鼠活動並模擬按鍵。",
    },
    "packages": [
        "rumps",
        "pynput",
        "PIL",
    ],
    "includes": [
        "ApplicationServices",
        "HIServices",
        "Quartz",
        "CoreFoundation",
        "ServiceManagement",
    ],
    "excludes": ["tkinter"],
}

setup(
    name="SimulateInput",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    install_requires=[],  # override pyproject.toml deps；py2app 0.28 從 venv 讀取，不需此欄位
)
