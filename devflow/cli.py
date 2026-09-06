"""命令行入口：devflow init / doctor / serve / add / list / show / approve / retry / ignore / send / digest"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    help="DevFlow AI — AI Delivery Agent（微信/钉钉需求 → GitHub Issue → Claude 编码 → CI/CD → AI 验证 → 交付客户）",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = typer.Option(None, "--config", "-c", help="config.yaml 路径（默认当前目录）")


def _utf8_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _setup_logging(cfg=None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg is not None:
        handlers.append(logging.FileHandler(cfg.data_path / "devflow.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load(config: str | None):
    from .config import load_config

    try:
        return load_config(config)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)


def _api(cfg):
    """服务在跑就返回 HTTP 客户端，否则 None。"""
    import httpx

    try:
        auth = (cfg.auth.username, cfg.auth.password) if cfg.auth.enabled else None
        c = httpx.Client(base_url=cfg.dashboard_url, timeout=60, auth=auth)
        c.get("/health").raise_for_status()
        return c
    except Exception:  # noqa: BLE001
        return None


def _local_pipeline(cfg):
    from .ai import AI
    from .pipeline import Pipeline
    from .store import Store

    return Pipeline(cfg, Store(cfg.db_path), AI(cfg), sync=True)


def _print_task(t) -> None:
    typer.echo(f"#{t.id} [{t.priority}] {t.title or t.raw_text[:30] or '(无标题)'}   状态: {t.label}")
    if t.project or t.customer or t.deadline:
        typer.echo(f"     项目: {t.project or '-'}   客户: {t.customer or '-'}   截止: {t.deadline or '-'}")
    if t.triage_reason:
        typer.echo(f"     AI: {t.triage_reason}")
    for u in (t.issue_url, t.pr_url):
        if u:
            typer.echo(f"     {u}")
    if t.error:
        typer.secho(f"     错误: {t.error}", fg="red")


# ---------------------------------------------------------------- commands
@app.command()
def init() -> None:
    """在当前目录生成 config.yaml / .env，然后做环境检查。"""
    _utf8_console()
    root = Path.cwd()
    pkg_root = Path(__file__).resolve().parent.parent
    for dst_name, src_name in (("config.yaml", "config.example.yaml"), (".env", ".env.example")):
        dst, src = root / dst_name, pkg_root / src_name
        if dst.exists():
            typer.echo(f"已存在，跳过: {dst}")
        elif src.exists():
            shutil.copy(src, dst)
            typer.secho(f"已生成: {dst}", fg="green")
    typer.echo("\n下一步：用编辑器打开 config.yaml，填好 projects（仓库路径、GitHub 仓库、部署地址）。\n")
    doctor(None)


@app.command()
def doctor(config: Optional[str] = ConfigOpt) -> None:
    """检查环境：Claude Code、gh、git、Playwright、配置文件、项目仓库。"""
    _utf8_console()
    from .integrations import github
    from .integrations.browser_test import chromium_ready
    from .integrations.claude_code import find_claude

    rows: list[tuple[bool, str, str]] = []
    rows.append((sys.version_info >= (3, 10), "Python", sys.version.split()[0]))
    rows.append((bool(shutil.which("git")), "git", shutil.which("git") or "未安装"))
    ok, msg = github.gh_ok()
    rows.append((ok, "gh CLI", msg))

    cfg = None
    try:
        from .config import load_config

        cfg = load_config(config)
        rows.append((True, "config.yaml", str(cfg.path)))
    except Exception as e:  # noqa: BLE001
        rows.append((False, "config.yaml", str(e)))

    exe = find_claude(cfg.coder.claude_path if cfg else "")
    rows.append((bool(exe), "Claude Code", exe or "未找到：npm install -g @anthropic-ai/claude-code"))
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    backend = "anthropic API（ANTHROPIC_API_KEY）" if has_key else ("claude-code（用本机 Claude 登录态）" if exe else "无可用后端")
    rows.append((has_key or bool(exe), "AI 后端", backend))
    ok, msg = chromium_ready()
    rows.append((ok, "Playwright Chromium", msg))

    if cfg:
        d = cfg.dingtalk
        if d.enabled:
            rows.append((bool(d.client_id and d.client_secret), "钉钉机器人",
                         "已配置 AppKey/AppSecret" if d.client_id and d.client_secret else "enabled 但缺 client_id / client_secret"))
        else:
            rows.append((True, "钉钉机器人", "未启用（可选）"))
        rows.append((True, "提醒渠道",
                     "钉钉/企业微信 webhook" if (d.notify_webhook or cfg.wecom.notify_webhook) else "仅桌面通知（可在 dingtalk.notify_webhook 配 webhook）"))
        if not cfg.projects:
            rows.append((False, "projects", "没有配置任何项目"))
        for p in cfg.projects:
            problems, notes = [], []
            if not p.repo_path:
                problems.append("缺 repo_path")
            elif not Path(p.repo_path).exists():
                if not p.github_repo:
                    problems.append(f"repo_path 不存在: {p.repo_path}")
                else:
                    notes.append(f"repo_path 不存在，首次用到时自动 clone {p.github_repo}")
            elif not github.is_git_repo(p.repo_path):
                problems.append("repo_path 不是 git 仓库")
            if not p.github_repo:
                problems.append("缺 github_repo")
            rows.append((not problems, f"项目 {p.name}",
                         "; ".join(problems + notes) or f"{p.github_repo} · {p.deploy_url or '无部署地址（跳过 AI 测试）'}"))

    for ok, name, detail in rows:
        typer.echo(f"{'✅' if ok else '❌'} {name:<20} {detail}")
    if all(r[0] for r in rows):
        typer.secho("\n环境就绪。启动：devflow serve", fg="green")
    else:
        typer.secho("\n请先处理 ❌ 项。", fg="yellow")


@app.command()
def serve(config: Optional[str] = ConfigOpt,
          no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开面板")) -> None:
    """启动服务：面板 + 钉钉机器人 + 剪贴板监听 + 交付流水线 + 定时提醒。"""
    _utf8_console()
    cfg = _load(config)
    _setup_logging(cfg)
    import uvicorn

    from .ai import AI
    from .notify import Notifier
    from .pipeline import Pipeline
    from .scheduler import start_scheduler
    from .server import create_app
    from .store import Store

    log = logging.getLogger("devflow")
    store = Store(cfg.db_path)
    notifier = Notifier(cfg)
    try:
        ai = AI(cfg)
    except Exception as e:  # noqa: BLE001
        typer.secho(f"AI 后端初始化失败：{e}", fg="red")
        raise typer.Exit(1)
    pipeline = Pipeline(cfg, store, ai, notifier)
    pipeline.start()

    if cfg.clipboard.enabled:
        from .inbox.clipboard import ClipboardWatcher

        ClipboardWatcher(
            pipeline.on_clipboard_text,
            min_chars=cfg.clipboard.min_chars, trigger_prefix=cfg.clipboard.trigger_prefix,
            ignore=pipeline.clipboard_ignore, on_image=pipeline.on_clipboard_image, apps=cfg.clipboard.apps,
        ).start()
    if cfg.dingtalk.enabled:
        if cfg.dingtalk.client_id and cfg.dingtalk.client_secret:
            from .inbox.dingtalk_bot import start_dingtalk_bot

            start_dingtalk_bot(cfg.dingtalk.client_id, cfg.dingtalk.client_secret, pipeline.on_dingtalk_message,
                               attach_dir=cfg.data_path / "attachments")
        else:
            log.warning("dingtalk.enabled=true 但没有 client_id/client_secret，钉钉机器人未启动")
    from .monitor import SiteMonitor

    monitor = SiteMonitor(cfg)
    monitor.start()  # 每 10 分钟检查各项目线上健康 + HTTPS 证书到期
    start_scheduler(cfg, store, notifier, monitor)

    # 上次中断的任务按状态接着跑
    resumed = pipeline.resume()
    if resumed:
        log.info("恢复了 %d 个中断的任务", resumed)

    web = create_app(cfg, store, pipeline, monitor)
    if cfg.dashboard.open_browser and not no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(cfg.dashboard_url)).start()
    typer.secho(f"\nDevFlow AI 运行中：{cfg.dashboard_url}   （Ctrl+C 退出）", fg="green")
    if cfg.auth.enabled:
        typer.echo(f"面板登录：用户名 {cfg.auth.username}（密码见 config.yaml 的 auth 段）\n")
    uvicorn.run(web, host=cfg.dashboard.host, port=cfg.dashboard.port, log_level="warning")


@app.command()
def add(text: str = typer.Argument(..., help="需求文本；传 - 则从标准输入读取"),
        project: str = typer.Option("", help="指定项目名（留空让 AI 判断）"),
        customer: str = typer.Option("", help="客户"),
        source: str = typer.Option("manual", help="wechat | dingtalk | manual"),
        config: Optional[str] = ConfigOpt) -> None:
    """手动加一条需求。服务在运行就交给服务处理，否则本地同步处理。"""
    _utf8_console()
    cfg = _load(config)
    if text == "-":
        text = sys.stdin.read()
    api = _api(cfg)
    if api:
        r = api.post("/api/inbox", json={"text": text, "project": project, "customer": customer, "source": source}).json()
        typer.echo(f"已提交 #{r['id']}，AI 分析中。面板：{cfg.dashboard_url}")
        return
    _setup_logging(cfg)
    t = _local_pipeline(cfg).ingest(text, source=source, project=project, customer=customer)
    _print_task(t)


@app.command("list")
def list_(all: bool = typer.Option(False, "--all", "-a", help="包含已完成/忽略"),
          config: Optional[str] = ConfigOpt) -> None:
    """列出任务。"""
    _utf8_console()
    cfg = _load(config)
    from .store import Store

    tasks = Store(cfg.db_path).list(limit=200)
    if not all:
        tasks = [t for t in tasks if not t.is_done]
    for t in tasks:
        _print_task(t)
    if not tasks:
        typer.echo("没有任务")


@app.command()
def show(task_id: int, config: Optional[str] = ConfigOpt) -> None:
    """查看任务详情（JSON）。"""
    _utf8_console()
    cfg = _load(config)
    from .store import Store

    t = Store(cfg.db_path).get(task_id)
    if not t:
        typer.secho("任务不存在", fg="red")
        raise typer.Exit(1)
    typer.echo(t.model_dump_json(indent=2))


def _act(action: str, task_id: int, cfg, body: dict | None = None) -> None:
    api = _api(cfg)
    if api:
        r = api.post(f"/api/tasks/{task_id}/{action}", json=body or {})
        if r.status_code >= 400:
            typer.secho(r.json().get("error", r.text), fg="red")
            raise typer.Exit(1)
        typer.echo(f"已执行: {r.json()['action']}")
        return
    _setup_logging(cfg)
    p = _local_pipeline(cfg)
    fns = {
        "approve": lambda: p.approve(task_id),
        "retry": lambda: p.retry(task_id, (body or {}).get("note", "")),
        "ignore": lambda: (p.ignore(task_id), "ignored")[1],
    }
    try:
        typer.echo(f"已执行: {fns[action]()}")
    except (ValueError, KeyError) as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)


@app.command()
def approve(task_id: int, config: Optional[str] = ConfigOpt) -> None:
    """确认当前步骤（建 Issue / 开始编码 / 合并 / 发送客户…）。"""
    _utf8_console()
    _act("approve", task_id, _load(config))


@app.command()
def retry(task_id: int, note: str = typer.Option("", help="补充说明，会追加到需求描述"),
          config: Optional[str] = ConfigOpt) -> None:
    """重试失败的步骤。"""
    _utf8_console()
    _act("retry", task_id, _load(config), {"note": note})


@app.command()
def ignore(task_id: int, config: Optional[str] = ConfigOpt) -> None:
    """忽略任务。"""
    _utf8_console()
    _act("ignore", task_id, _load(config))


@app.command()
def resolve(task_id: int, note: str = typer.Option("", help="一句说明，会写进 Issue"),
            config: Optional[str] = ConfigOpt) -> None:
    """我自己已经解决了：关闭 DevFlow 开的 PR/分支/Issue，任务标记已交付。"""
    _utf8_console()
    cfg = _load(config)
    api = _api(cfg)
    if api:
        r = api.post(f"/api/tasks/{task_id}/resolve", json={"note": note})
        if r.status_code >= 400:
            typer.secho(r.json().get("error", r.text), fg="red")
            raise typer.Exit(1)
        typer.echo(f"已执行: {r.json()['action']}")
        return
    _setup_logging(cfg)
    done = _local_pipeline(cfg).resolve(task_id, note=note)
    typer.echo("已标记解决；" + ("、".join(done) or "无需清理"))


@app.command()
def send(task_id: int, config: Optional[str] = ConfigOpt) -> None:
    """把回复发给客户（钉钉直接发；微信复制到剪贴板）。"""
    _utf8_console()
    cfg = _load(config)
    from .store import Store

    t = Store(cfg.db_path).get(task_id)
    if not t or t.state != "ready_to_deliver":
        typer.secho("任务不在「待发送客户」状态", fg="red")
        raise typer.Exit(1)
    _act("approve", task_id, cfg)


@app.command()
def digest(config: Optional[str] = ConfigOpt,
           send_: bool = typer.Option(False, "--send", help="同时推送到提醒渠道")) -> None:
    """打印"还有哪些没做"的日报。"""
    _utf8_console()
    cfg = _load(config)
    from .notify import Notifier
    from .scheduler import build_digest
    from .store import Store

    from .monitor import SiteMonitor

    monitor = SiteMonitor(cfg)
    try:
        monitor.refresh()
    except Exception as e:  # noqa: BLE001
        typer.secho(f"线上监控检查失败：{e}", fg="yellow")
    title, body = build_digest(Store(cfg.db_path), cfg, monitor)
    typer.echo(f"{title}\n\n{body}")
    if send_:
        Notifier(cfg).me(title, body)


if __name__ == "__main__":
    app()
