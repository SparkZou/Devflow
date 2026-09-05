"""给开发者自己发提醒：钉钉/企业微信 webhook + Windows 桌面通知 + 日志。"""
from __future__ import annotations

import logging
import os
import subprocess

from .config import Config
from .integrations import dingtalk, wecom

log = logging.getLogger("devflow.notify")

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName("text")
$n.Item(0).AppendChild($t.CreateTextNode($env:DF_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:DF_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("DevFlow AI").Show($toast)
"""


def toast(title: str, body: str) -> None:
    if os.name != "nt":
        return
    try:
        env = dict(os.environ, DF_TITLE=title[:80], DF_BODY=body[:200])
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST_PS],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("toast 失败: %s", e)


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def me(self, title: str, markdown: str, desktop: bool = True) -> None:
        """通知开发者本人。"""
        log.info("🔔 %s | %s", title, markdown.replace("\n", " ")[:200])
        d = self.cfg.dingtalk
        if d.notify_webhook:
            dingtalk.send_webhook_markdown(d.notify_webhook, d.notify_secret, title, f"### {title}\n\n{markdown}")
        if self.cfg.wecom.notify_webhook:
            wecom.send_webhook_markdown(self.cfg.wecom.notify_webhook, f"**{title}**\n{markdown}")
        if desktop and self.cfg.digest.toast:
            toast(title, markdown)
