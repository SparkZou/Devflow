"""Claude Code 无人值守模式（`claude -p`）封装：用于自动写代码，也可作为 AI 后端。"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("devflow.claude_code")

SUBTYPE_ZH = {
    "error_max_turns": "达到最大轮数仍未完成",
    "error_during_execution": "执行过程中出错",
    "error_max_budget_usd": "超出费用上限",
}


def _version_key(p: Path) -> tuple:
    m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(p))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_claude(explicit: str = "") -> Optional[str]:
    """查找 claude 可执行文件：显式路径 > PATH > VS Code / Cursor 扩展内置 > npm 全局。"""
    if explicit:
        if Path(explicit).exists():
            return str(Path(explicit))
        log.warning("coder.claude_path 不存在: %s，改为自动查找", explicit)
    w = shutil.which("claude")
    if w:
        return w
    exe = "claude.exe" if os.name == "nt" else "claude"
    candidates: list[Path] = []
    for base in (
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
        Path.home() / ".cursor" / "extensions",
    ):
        if base.exists():
            for d in base.glob("anthropic.claude-code-*"):
                p = d / "resources" / "native-binary" / exe
                if p.exists():
                    candidates.append(p)
    for p in (
        Path.home() / ".local" / "bin" / exe,
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ):
        if p.exists():
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=_version_key)
    return str(candidates[-1])


def _clean_env() -> dict:
    # 去掉嵌套会话标记，否则在 Claude Code 内部启动 claude 会被拒绝
    return {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}


@dataclass
class CoderResult:
    ok: bool
    result: str = ""
    session_id: str = ""
    subtype: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    denials: list[str] = field(default_factory=list)  # 因权限被拒的工具调用（人能看懂的摘要）
    raw: dict = field(default_factory=dict)
    stderr: str = ""

    @property
    def error_summary(self) -> str:
        """给人看的失败原因。"""
        why = SUBTYPE_ZH.get(self.subtype, self.subtype or "未知错误")
        s = f"{why}（{self.num_turns} 轮，${self.cost_usd:.2f}）"
        if self.denials:
            shown = "；".join(self.denials[:5])
            s += f"。有 {len(self.denials)} 次命令因权限被拒，例如：{shown}"
            s += "。可在 config.yaml 的 coder.allowed_tools 放开，或设 coder.skip_permissions: true"
        text = (self.result or "").strip()
        if text and not text.startswith("{"):
            s += f"。最后输出：{text[-300:]}"
        return s


def _summarize_denial(d: dict) -> str:
    name = d.get("tool_name", "?")
    inp = d.get("tool_input") or {}
    cmd = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
    return f"{name}: {str(cmd)[:80]}" if cmd else name


def parse_output(stdout: str, returncode: int, stderr: str = "") -> CoderResult:
    out = (stdout or "").strip()
    data: dict = {}
    if out:
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # 偶尔前面会混入日志行，取最后一个 JSON 对象
            m = re.search(r"\{.*\}\s*$", out, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = {}
    if returncode != 0 and not data:
        raise RuntimeError(f"claude 退出码 {returncode}: {(stderr or out)[-2000:]}")
    result = data.get("result")
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False) if result else ("" if data else out)
    ok = not data.get("is_error", returncode != 0)
    denials = [_summarize_denial(d) for d in (data.get("permission_denials") or []) if isinstance(d, dict)]
    return CoderResult(
        ok=ok,
        result=result,
        session_id=data.get("session_id", ""),
        subtype=str(data.get("subtype", "")),
        cost_usd=float(data.get("total_cost_usd") or 0),
        num_turns=int(data.get("num_turns") or 0),
        denials=denials,
        raw=data,
        stderr=(stderr or "")[-2000:],
    )


def run_claude(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    exe: str | None = None,
    allowed_tools: str = "",
    max_turns: int = 1,
    model: str = "",
    permission_mode: str = "",
    skip_permissions: bool = False,
    timeout_s: int = 900,
    resume: str = "",
) -> CoderResult:
    exe = exe or find_claude()
    if not exe:
        raise RuntimeError(
            "找不到 claude 可执行文件。请安装 Claude Code（npm install -g @anthropic-ai/claude-code）"
            "或在 config.yaml 的 coder.claude_path 指定路径。"
        )
    cmd = [exe, "-p", "--output-format", "json", "--max-turns", str(max_turns)]
    if resume:
        cmd += ["--resume", resume]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    elif permission_mode:
        cmd += ["--permission-mode", permission_mode]
    cwd = str(cwd) if cwd else tempfile.gettempdir()
    log.info("claude -p (cwd=%s, tools=%s, max_turns=%s%s)", cwd, allowed_tools or "-", max_turns,
             f", resume={resume[:8]}" if resume else "")
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=_clean_env(),
        timeout=timeout_s,
    )
    res = parse_output(proc.stdout, proc.returncode, proc.stderr)
    if res.denials:
        log.warning("Claude Code 有 %d 次工具调用因权限被拒：%s", len(res.denials), "; ".join(res.denials[:5]))
    return res
