"""Async wrapper around the `nblm` (notebooklm-py) CLI.

All calls pass an explicit --notebook UUID so concurrent Telegram users never
clobber each other via the shared ~/.notebooklm/context.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)

NBLM = os.environ.get("NBLM_BIN", "/home/ubuntu/.local/bin/nblm")

# nblm 是 bash 包裝腳本，內部呼叫 uv；確保子行程 PATH 找得到 uv。
_EXTRA_PATH = "/home/ubuntu/.local/bin"
if _EXTRA_PATH not in os.environ.get("PATH", "").split(":"):
    os.environ["PATH"] = f"{_EXTRA_PATH}:{os.environ.get('PATH', '')}"
os.environ.setdefault("HOME", "/home/ubuntu")


class NblmError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass
class Artifact:
    id: str
    title: str
    type: str
    status: str


async def _run(args: list[str], timeout: int = 180) -> str:
    cmd = [NBLM, *args]
    log.info("nblm exec: %s", shlex.join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise NblmError(f"指令逾時（{timeout}s）", exit_code=2)

    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    if proc.returncode != 0:
        log.warning("nblm failed rc=%s stderr=%s", proc.returncode, stderr[-800:])
        raise NblmError(_friendly_error(stderr), exit_code=proc.returncode or 1, stderr=stderr)
    return stdout


def _friendly_error(stderr: str) -> str:
    s = stderr.lower()
    # 環境問題優先判斷 —— 否則會被誤譯成業務錯誤，掩蓋真因
    if "command not found" in s or ": not found" in s or "no such file or directory" in s:
        return (f"執行環境異常（找不到可執行檔）。請檢查 PATH 設定。\n"
                f"原始訊息：{stderr.strip().splitlines()[-1][:200]}")
    if "permission denied" in s:
        return "執行權限不足，請檢查 nblm 腳本權限。"
    if "auth" in s or "cookie" in s or "expired" in s or "login" in s:
        return "NotebookLM 認證已失效，需要重新匯入 cookie。"
    if "no result found for rpc" in s or "rate" in s or "quota" in s:
        return "Google 目前限流，請等 5-10 分鐘後重試。"
    if "not found" in s:
        return "找不到指定的筆記本或資源。"
    tail = stderr.strip().splitlines()
    return tail[-1][:300] if tail else "未知錯誤"


def _parse_json(raw: str) -> dict:
    """nblm may emit rich-console noise before JSON; grab the first JSON object."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = raw.find("{", start + 1)
    raise NblmError("無法解析 nblm 輸出")


# ---------------------------------------------------------------- notebooks

async def list_notebooks() -> list[dict]:
    data = _parse_json(await _run(["list", "--json"], timeout=120))
    return data.get("notebooks", [])


async def list_sources(notebook_id: str) -> list[dict]:
    data = _parse_json(
        await _run(["source", "list", "--notebook", notebook_id, "--json"], timeout=120)
    )
    return data.get("sources", [])


async def create_notebook(title: str) -> dict:
    data = _parse_json(await _run(["create", title, "--json"], timeout=120))
    # v0.8.0 回傳 {"notebook": {...}}；舊版為扁平結構，兩者都吃
    return data.get("notebook", data)


async def add_source(notebook_id: str, content: str, type_: str | None = None,
                     title: str | None = None) -> dict:
    args = ["source", "add", content, "--notebook", notebook_id, "--json",
            "--request-timeout", "120"]
    if type_:
        args += ["--type", type_]
    if title:
        args += ["--title", title]
    data = _parse_json(await _run(args, timeout=420))
    return data.get("source", data)


async def add_research(notebook_id: str, query: str, mode: str = "fast") -> dict:
    """Web research → import all found sources. fast≈1min, deep≈15-30min."""
    args = ["source", "add-research", query, "--notebook", notebook_id,
            "--mode", mode, "--import-all", "--json",
            "--timeout", "1800" if mode == "deep" else "300"]
    raw = await _run(args, timeout=2000 if mode == "deep" else 400)
    try:
        return _parse_json(raw)
    except NblmError:
        return {"raw": raw.strip()[-500:]}


async def delete_source(notebook_id: str, source_id: str) -> None:
    await _run(["source", "delete", source_id, "--notebook", notebook_id, "-y"], timeout=120)


async def source_status(notebook_id: str, source_id: str) -> str:
    for s in await list_sources(notebook_id):
        sid = s.get("id", "")
        if sid == source_id or sid.startswith(source_id) or source_id.startswith(sid):
            return (s.get("status") or "unknown").lower()
    return "unknown"


# ---------------------------------------------------------------- chat

async def ask(notebook_id: str, question: str, conversation_id: str | None = None) -> dict:
    args = ["ask", question, "--notebook", notebook_id, "--json"]
    if conversation_id:
        args += ["-c", conversation_id]
    return _parse_json(await _run(args, timeout=300))


# ---------------------------------------------------------------- generate

async def generate(notebook_id: str, kind: str, instructions: str = "", **opts) -> dict:
    """kind: audio|video|slide-deck|infographic|report|mind-map|data-table|quiz|flashcards

    回傳 dict 可能是：
      * 非同步任務 → 含 artifact_id / task_id / id
      * 同步製品（mind-map）→ 直接含完整內容（mind_map / note_id / kind）
    """
    args = ["generate", kind]
    if instructions:
        args.append(instructions)
    args += ["--notebook", notebook_id, "--json"]
    for key, val in opts.items():
        if val:
            args += [f"--{key.replace('_', '-')}", str(val)]
    data = _parse_json(await _run(args, timeout=300))
    return data.get("artifact", data)


def extract_artifact_id(data: dict) -> str:
    """從 generate 回傳中挖出任務 ID；同步製品回空字串。"""
    for key in ("artifact_id", "task_id", "id", "artifactId", "taskId"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    # 有些版本再包一層
    for sub in ("artifact", "task", "result"):
        inner = data.get(sub)
        if isinstance(inner, dict):
            got = extract_artifact_id(inner)
            if got:
                return got
    return ""


def is_sync_payload(data: dict) -> bool:
    """mind-map 等同步製品：generate 當下就回完整內容。"""
    return any(k in data for k in ("mind_map", "mindMap", "content", "markdown"))


async def list_artifacts(notebook_id: str) -> list[Artifact]:
    data = _parse_json(
        await _run(["artifact", "list", "--notebook", notebook_id, "--json"], timeout=120)
    )
    return [
        Artifact(
            id=a.get("id", ""),
            title=a.get("title", ""),
            type=a.get("type", ""),
            status=(a.get("status") or "unknown").lower(),
        )
        for a in data.get("artifacts", [])
    ]


async def artifact_status(notebook_id: str, artifact_id: str) -> str:
    for a in await list_artifacts(notebook_id):
        if a.id == artifact_id or artifact_id.startswith(a.id) or a.id.startswith(artifact_id):
            return a.status
    return "unknown"


async def download(notebook_id: str, kind: str, artifact_id: str, dest: str,
                   fmt: str | None = None) -> str:
    args = ["download", kind, dest, "-n", notebook_id, "-a", artifact_id]
    if fmt:
        args += ["--format", fmt]
    await _run(args, timeout=600)
    return dest


async def auth_ok() -> bool:
    try:
        await _run(["auth", "check"], timeout=60)
        return True
    except NblmError:
        return False
