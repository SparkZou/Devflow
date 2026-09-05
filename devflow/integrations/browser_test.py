"""用 Playwright 打开部署地址：截图 + 收集控制台错误，交给 AI 判定。"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("devflow.browser")


def _scroll_through(page, step: int = 600, pause_ms: int = 150) -> None:
    """分段滚到页面底部再回到顶部，让 IntersectionObserver 类动画和懒加载内容都渲染出来。"""
    try:
        y, height = 0, page.evaluate("document.body.scrollHeight")
        while y < height and y < 60000:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(pause_ms)
            y += step
            height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


def run_browser_check(url: str, screenshot_path: str | Path, wait_ms: int = 4000) -> dict:
    info: dict = {
        "ok": False, "url": url, "status": None, "title": "",
        "console_errors": [], "error": "", "text_excerpt": "",
    }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        info["error"] = "未安装 playwright（pip install playwright && playwright install chromium）"
        return info
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("console", lambda m: info["console_errors"].append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: info["console_errors"].append(str(e)))
            resp = page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(wait_ms)
            _scroll_through(page)  # 触发滚动显现动画 / 懒加载，否则整页截图中间是空白
            info["status"] = resp.status if resp else None
            info["title"] = page.title()
            try:
                info["text_excerpt"] = page.inner_text("body")[:8000]
            except Exception:  # noqa: BLE001
                pass
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            browser.close()
        info["ok"] = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            msg = "Chromium 未安装，请运行: playwright install chromium"
        info["error"] = msg[:800]
    info["console_errors"] = info["console_errors"][:20]
    return info


def chromium_ready() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "未安装 playwright"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True, "Chromium 可用"
    except Exception as e:  # noqa: BLE001
        return False, "Chromium 未安装，运行: playwright install chromium" if "Executable" in str(e) else str(e)[:200]
