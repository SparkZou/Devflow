"""GitHub / git 操作。全部通过已登录的 `gh` CLI 和 `git` 完成，不需要额外 token。"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("devflow.github")


def _run(cmd: list[str], cwd: str | Path | None = None, check: bool = True, timeout: int = 300) -> str:
    proc = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} 失败: {(proc.stderr or proc.stdout).strip()[-1500:]}")
    return (proc.stdout or "").strip()


def gh(args: list[str], cwd=None, check=True, timeout=300) -> str:
    return _run(["gh", *args], cwd=cwd, check=check, timeout=timeout)


def gh_json(args: list[str], cwd=None):
    out = gh(args, cwd=cwd)
    return json.loads(out) if out else None


def git(args: list[str], cwd, check=True, timeout=300) -> str:
    return _run(["git", *args], cwd=cwd, check=check, timeout=timeout)


def gh_ok() -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, "未安装 gh CLI（https://cli.github.com）"
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return False, "gh 未登录，请运行 `gh auth login`"
    text = (proc.stdout or proc.stderr or "").strip()
    m = re.search(r"Logged in to \S+ account (\S+)", text)
    return True, f"已登录 GitHub 账号 {m.group(1)}" if m else "已登录"


def _tmp_md(body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        return f.name


# ---------------------------------------------------------------- issues / PRs
def create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> tuple[int, str]:
    body_file = _tmp_md(body)
    args = ["issue", "create", "-R", repo, "--title", title, "--body-file", body_file]
    try:
        if labels:
            try:
                url = gh(args + ["--label", ",".join(labels)])
            except RuntimeError as e:
                if "label" in str(e).lower() or "not found" in str(e).lower():
                    url = gh(args)  # 标签不存在则不带标签重试
                else:
                    raise
        else:
            url = gh(args)
    finally:
        Path(body_file).unlink(missing_ok=True)
    m = re.search(r"/issues/(\d+)", url)
    return (int(m.group(1)) if m else 0), url.strip()


def comment_issue(repo: str, number: int, body: str) -> None:
    body_file = _tmp_md(body)
    try:
        gh(["issue", "comment", str(number), "-R", repo, "--body-file", body_file], check=False)
    finally:
        Path(body_file).unlink(missing_ok=True)


def close_issue(repo: str, number: int, comment: str = "") -> None:
    args = ["issue", "close", str(number), "-R", repo]
    if comment:
        args += ["--comment", comment]
    gh(args, check=False)


def create_pr(repo: str, base: str, head: str, title: str, body: str) -> tuple[int, str]:
    body_file = _tmp_md(body)
    try:
        url = gh(["pr", "create", "-R", repo, "--base", base, "--head", head,
                  "--title", title, "--body-file", body_file])
    finally:
        Path(body_file).unlink(missing_ok=True)
    m = re.search(r"/pull/(\d+)", url)
    return (int(m.group(1)) if m else 0), url.strip()


def _summarize_checks(rollup: list | None) -> tuple[str, list[str]]:
    """返回 (none|pending|success|failure, 失败的检查名)."""
    if not rollup:
        return "none", []
    failed: list[str] = []
    pending = False
    for c in rollup:
        if c.get("__typename") == "CheckRun":
            if c.get("status") != "COMPLETED":
                pending = True
            elif (c.get("conclusion") or "").upper() in (
                "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
            ):
                failed.append(c.get("name") or "check")
        else:
            state = (c.get("state") or "").upper()
            if state in ("PENDING", "EXPECTED"):
                pending = True
            elif state in ("FAILURE", "ERROR"):
                failed.append(c.get("context") or "status")
    if failed:
        return "failure", failed
    if pending:
        return "pending", []
    return "success", []


def pr_status(repo: str, number: int) -> dict:
    data = gh_json(["pr", "view", str(number), "-R", repo, "--json",
                    "state,mergedAt,mergeCommit,statusCheckRollup,url,headRefOid,headRefName"])
    checks, failed = _summarize_checks(data.get("statusCheckRollup"))
    merge_commit = data.get("mergeCommit") or {}
    return {
        "state": data.get("state"),  # OPEN | MERGED | CLOSED
        "merged": bool(data.get("mergedAt")),
        "merged_at": data.get("mergedAt") or "",
        "merge_sha": merge_commit.get("oid", ""),
        "head_sha": data.get("headRefOid", ""),
        "head_ref": data.get("headRefName", ""),
        "checks": checks,
        "failed": failed,
        "url": data.get("url", ""),
    }


def pr_summary(repo: str, number: int) -> tuple[str, str]:
    """(提交列表, 变更文件列表) 用于生成 Release Note。"""
    d = gh_json(["pr", "view", str(number), "-R", repo, "--json", "commits,files"]) or {}
    commits = "\n".join(f"- {c.get('messageHeadline')}" for c in d.get("commits") or [])
    files = "\n".join(
        f"- {f.get('path')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})" for f in d.get("files") or []
    )
    return commits, files


def failed_run_logs(repo: str, branch: str, max_lines: int = 200) -> str:
    runs = gh_json(["run", "list", "-R", repo, "--branch", branch, "--limit", "5",
                    "--json", "databaseId,conclusion,name,headSha"]) or []
    chunks = []
    for r in runs:
        if r.get("conclusion") in ("failure", "timed_out", "cancelled"):
            log_txt = gh(["run", "view", str(r["databaseId"]), "-R", repo, "--log-failed"], check=False)
            lines = log_txt.splitlines()[-max_lines:]
            chunks.append(f"### workflow: {r.get('name')}\n" + "\n".join(lines))
    return "\n\n".join(chunks)[-12000:]


def merge_pr(repo: str, number: int) -> str:
    gh(["pr", "merge", str(number), "-R", repo, "--squash", "--delete-branch"], timeout=180)
    return pr_status(repo, number).get("merge_sha", "")


def find_workflow_run(repo: str, workflow: str, sha: str) -> Optional[dict]:
    runs = gh_json(["run", "list", "-R", repo, "--workflow", workflow, "--limit", "15",
                    "--json", "databaseId,status,conclusion,url,headSha,createdAt"]) or []
    for r in runs:
        if r.get("headSha") == sha:
            return r
    return None


# ---------------------------------------------------------------- git worktree
def is_git_repo(path: str | Path) -> bool:
    try:
        return git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False) == "true"
    except Exception:  # noqa: BLE001
        return False


def prepare_worktree(repo_path: str, base_branch: str, branch: str, worktree: str | Path,
                     from_existing: bool = False) -> None:
    """在独立 worktree 里建分支，不影响你正在用的工作目录。"""
    worktree = Path(worktree)
    git(["fetch", "origin", "--prune"], cwd=repo_path)
    if worktree.exists():
        git(["worktree", "remove", "--force", str(worktree)], cwd=repo_path, check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    git(["worktree", "prune"], cwd=repo_path, check=False)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    start = f"origin/{branch}" if from_existing else f"origin/{base_branch}"
    git(["worktree", "add", "-B", branch, str(worktree), start], cwd=repo_path)


def remove_worktree(repo_path: str, worktree: str | Path) -> None:
    git(["worktree", "remove", "--force", str(worktree)], cwd=repo_path, check=False)
    shutil.rmtree(worktree, ignore_errors=True)
    git(["worktree", "prune"], cwd=repo_path, check=False)


def delete_local_branch(repo_path: str, branch: str) -> None:
    git(["branch", "-D", branch], cwd=repo_path, check=False)


def delete_remote_branch(repo_path: str, branch: str) -> bool:
    if not git(["ls-remote", "--heads", "origin", branch], cwd=repo_path, check=False):
        return False
    git(["push", "origin", "--delete", branch], cwd=repo_path, check=False, timeout=120)
    return True


def close_pr(repo: str, number: int, comment: str = "") -> None:
    args = ["pr", "close", str(number), "-R", repo, "--delete-branch"]
    if comment:
        args += ["--comment", comment]
    gh(args, check=False)


def has_changes(worktree: str | Path) -> bool:
    return bool(git(["status", "--porcelain"], cwd=worktree))


def commit_and_push(worktree: str | Path, branch: str, message: str) -> str:
    git(["add", "-A"], cwd=worktree)
    git(["commit", "-m", message], cwd=worktree)
    git(["push", "-u", "origin", branch, "--force-with-lease"], cwd=worktree, timeout=300)
    return git(["rev-parse", "HEAD"], cwd=worktree)


def push_branch(worktree: str | Path, branch: str) -> str:
    git(["push", "-u", "origin", branch, "--force-with-lease"], cwd=worktree, timeout=300)
    return git(["rev-parse", "HEAD"], cwd=worktree)


def diff_stat(worktree: str | Path, base_branch: str) -> str:
    return git(["diff", "--stat", f"origin/{base_branch}...HEAD"], cwd=worktree, check=False)


def commit_log(worktree: str | Path, base_branch: str) -> str:
    return git(["log", "--oneline", f"origin/{base_branch}..HEAD"], cwd=worktree, check=False)
