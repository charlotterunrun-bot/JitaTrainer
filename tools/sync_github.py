"""JitaTrainer GitHub 同步工具。

职责：
  1. 确保远程仓库存在（不存在则通过 API 创建）
  2. 把本地分支与里程碑标签推送到 GitHub

用法：
    python tools/sync_github.py ensure              # 仅确保远程仓库存在
    python tools/sync_github.py push [分支名]        # 推送分支（默认 main）与全部标签
    python tools/sync_github.py tag <标签名>         # 创建附注标签并推送
    python tools/sync_github.py status              # 显示远程与本地状态

凭据：
    优先读取环境变量 GITHUB_TOKEN，其次读取 .secrets/github_token.txt。
    令牌**永远不会**被打印，也不写入 .git/config：推送时通过一次性的
    GIT_CONFIG_* 环境变量注入认证头（详见 git() 的说明）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "charlotterunrun-bot"
REPO = "JitaTrainer"
DESCRIPTION = "吉他训练器 / Guitar fretboard & ear trainer (Windows, Python, portable)"

ROOT = Path(__file__).resolve().parent.parent
SECRET_DIR = ROOT / ".secrets"
TOKEN_FILE = SECRET_DIR / "github_token.txt"
REMOTE_URL = f"https://github.com/{OWNER}/{REPO}.git"


# --------------------------------------------------------------------------
# 凭据
# --------------------------------------------------------------------------
def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="ascii").strip()
        if token:
            return token
    raise SystemExit(
        f"未找到 GitHub 访问令牌。请设置环境变量 GITHUB_TOKEN，"
        f"或把令牌写入 {TOKEN_FILE}（该目录已被 .gitignore 排除）。"
    )


def mask(text: str, token: str) -> str:
    """输出前彻底遮蔽令牌，避免任何日志/终端泄漏。"""
    return text.replace(token, "***") if token else text


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------
def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = "https://api.github.com" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "jitatrainer-sync")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def ensure_repo(token: str) -> None:
    status, data = api("GET", f"/repos/{OWNER}/{REPO}", token)
    if status == 200 and isinstance(data, dict):
        print(f"远程仓库已存在：{data.get('full_name')}（private={data.get('private')}）")
        return
    if status not in (404, 0):
        raise SystemExit(f"查询仓库失败：HTTP {status} {data}")

    print(f"远程仓库不存在，正在创建 {OWNER}/{REPO} …")
    status, data = api(
        "POST",
        "/user/repos",
        token,
        {
            "name": REPO,
            "description": DESCRIPTION,
            "private": False,
            "has_issues": True,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
        },
    )
    if status == 201 and isinstance(data, dict):
        print(f"创建成功：{data.get('html_url')}")
    else:
        raise SystemExit(f"创建仓库失败：HTTP {status} {data}")


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------
def git(*args: str, token: str, check: bool = True) -> subprocess.CompletedProcess:
    """执行 git，通过**环境变量**注入认证头。

    为什么不用 credential.helper：
      - 系统默认 helper 是 manager，它需要启动 shell 来提示凭据；
      - 在受限沙箱中 git 无法创建管道，任何走 shell 的凭据助手都会失败。
    因此改为把 `http.extraheader` 通过 GIT_CONFIG_* 环境变量传入：
      - 令牌不进入命令行参数（不会出现在进程列表里）
      - 令牌不写入 .git/config、不落盘任何凭据文件
      - 只对这一次 git 调用生效
    """
    auth = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
    env["GIT_TERMINAL_PROMPT"] = "0"  # 禁止任何交互式提示，失败即失败

    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    raw = (proc.stdout or "") + (proc.stderr or "")
    out = mask(raw, token).replace(auth, "***").strip()
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败（exit {proc.returncode}）：\n{out}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, "")


def current_branch(token: str) -> str:
    proc = git("rev-parse", "--abbrev-ref", "HEAD", token=token)
    return proc.stdout.strip() or "main"


def ensure_remote(token: str) -> None:
    proc = git("remote", "get-url", "origin", token=token, check=False)
    if proc.returncode != 0:
        print(f"添加远程 origin → {REMOTE_URL}")
        git("remote", "add", "origin", REMOTE_URL, token=token)
    elif proc.stdout.strip() != REMOTE_URL:
        print(f"修正远程 origin → {REMOTE_URL}（原：{proc.stdout.strip()}）")
        git("remote", "set-url", "origin", REMOTE_URL, token=token)


def cmd_ensure(token: str) -> None:
    ensure_repo(token)
    ensure_remote(token)


def cmd_push(token: str, branch: str | None = None) -> None:
    ensure_repo(token)
    ensure_remote(token)
    branch = branch or current_branch(token)
    print(f"推送分支 {branch} 与标签 …")
    git("push", "-u", "origin", branch, token=token)
    git("push", "origin", "--tags", token=token)
    print(f"完成：https://github.com/{OWNER}/{REPO}/tree/{branch}")


def cmd_tag(token: str, name: str) -> None:
    ensure_remote(token)
    git("tag", "-a", name, "-m", f"里程碑 {name}", token=token)
    git("push", "origin", name, token=token)
    print(f"标签已推送：{name}")


def cmd_status(token: str) -> None:
    ensure_remote(token)
    print(git("status", "-sb", token=token).stdout)
    print(git("remote", "-v", token=token).stdout)
    print(git("tag", "-l", token=token, check=False).stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JitaTrainer GitHub 同步工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ensure", help="确保远程仓库存在")
    p_push = sub.add_parser("push", help="推送分支与标签")
    p_push.add_argument("branch", nargs="?", default=None)
    p_tag = sub.add_parser("tag", help="创建并推送里程碑标签")
    p_tag.add_argument("name")
    sub.add_parser("status", help="显示仓库状态")
    args = parser.parse_args(argv)

    token = load_token()
    if args.command == "ensure":
        cmd_ensure(token)
    elif args.command == "push":
        cmd_push(token, args.branch)
    elif args.command == "tag":
        cmd_tag(token, args.name)
    elif args.command == "status":
        cmd_status(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
