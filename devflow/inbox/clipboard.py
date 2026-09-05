"""剪贴板监听：在微信/钉钉里复制客户消息或截图 → 自动送入收件箱。

个人微信没有官方机器人 API（第三方协议有封号风险），所以用"复制即收件"的方式：
- 复制文字：Ctrl+C 后 1 秒内送 AI 判定是不是需求
- 复制图片（截图）：自动附到刚收到的任务上，或留给接下来复制的文字
- 只接受前台窗口是微信/钉钉（config: clipboard.apps）时的复制，在 VS Code / 浏览器里复制的东西不收
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("devflow.clipboard")

_URL_OR_PATH = re.compile(r"^\S+$")  # 单个 token：URL / 路径 / 命令，不当需求
_SOURCE_BY_APP = {"dingtalk.exe": "dingtalk", "dingtalk": "dingtalk"}


def foreground_exe() -> str:
    """当前前台窗口所属进程的 exe 文件名（仅 Windows）；识别失败返回 ""。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def accept_app(exe: str, apps: list[str]) -> bool:
    """apps 为空 = 任何程序都收；识别不出前台程序也收（宁可多收）。"""
    if not apps or not exe:
        return True
    return exe.lower() in {a.lower() for a in apps}


def source_for(exe: str) -> str:
    return _SOURCE_BY_APP.get(exe.lower(), "wechat")


def _grab_image() -> Optional[bytes]:
    """剪贴板里是图片就返回 PNG bytes（仅 Windows/macOS 支持）。"""
    try:
        from PIL import Image, ImageGrab

        img = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(img, Image.Image):
        return None
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


class ClipboardWatcher:
    def __init__(self, on_text: Callable[[str, str], None], min_chars: int = 12,
                 trigger_prefix: str = "", interval: float = 0.8, ignore: set[str] | None = None,
                 on_image: Callable[[bytes, str], None] | None = None, apps: list[str] | None = None):
        self.on_text = on_text
        self.on_image = on_image
        self.min_chars = min_chars
        self.trigger_prefix = trigger_prefix
        self.interval = interval
        self.apps = list(apps or [])
        self.ignore = ignore if ignore is not None else set()  # 我们自己放进剪贴板的内容，不要再吃回来
        self._last: str | None = None
        self._last_img = ""
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="clipboard", daemon=True)

    def start(self) -> "ClipboardWatcher":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _from_allowed_app(self) -> tuple[bool, str]:
        exe = foreground_exe()
        if not accept_app(exe, self.apps):
            log.debug("忽略来自 %s 的剪贴板内容", exe)
            return False, exe
        return True, exe

    def _check_image(self) -> bool:
        """剪贴板里是图片就处理并返回 True（不管是否接受）。"""
        if not self.on_image:
            return False
        png = _grab_image()
        if not png:
            return False
        digest = hashlib.md5(png).hexdigest()
        if digest == self._last_img:
            return True
        self._last_img = digest
        ok, exe = self._from_allowed_app()
        if not ok:
            return True
        try:
            self.on_image(png, source_for(exe))
        except Exception:  # noqa: BLE001
            log.exception("处理剪贴板图片失败")
        return True

    def _loop(self) -> None:
        import pyperclip

        try:
            self._last = pyperclip.paste()
        except Exception:  # noqa: BLE001
            self._last = ""
        if self.on_image:
            self._last_img = hashlib.md5(_grab_image() or b"").hexdigest()  # 启动时已有的图片不算
        log.info("剪贴板监听已启动（min_chars=%s, prefix=%r, 图片=%s, 只收 %s）",
                 self.min_chars, self.trigger_prefix, bool(self.on_image), self.apps or "所有程序")
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self._check_image():
                continue
            try:
                cur = pyperclip.paste()
            except Exception:  # noqa: BLE001
                continue
            if cur is None or cur == self._last:
                continue
            self._last = cur
            text = cur.strip()
            if not text or text in self.ignore:
                continue
            if self.trigger_prefix:
                if not text.startswith(self.trigger_prefix):
                    continue
                text = text[len(self.trigger_prefix):].strip()
            if len(text) < self.min_chars or _URL_OR_PATH.match(text):
                continue
            ok, exe = self._from_allowed_app()
            if not ok:
                continue
            try:
                self.on_text(text, source_for(exe))
            except Exception:  # noqa: BLE001
                log.exception("处理剪贴板内容失败")
