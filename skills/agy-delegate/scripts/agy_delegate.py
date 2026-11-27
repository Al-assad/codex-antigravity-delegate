#!/usr/bin/env python3
"""Interactive Codex → Antigravity Desktop delegation client."""

import argparse
import datetime
from contextlib import contextmanager
from collections import deque
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid


MODELS = {"neon": "gemini-4-neon-high", "neon-xhigh": "gemini-4-neon-xhigh",
          "neon-medium": "gemini-4-neon-medium", "carbon": "gemini-4-carbon-high",
          "argon": "gemini-4-argon-high", "flash": "gemini-3.8-flash-high"}
MAX_FRAME = 256 * 1024
MAX_PROMPT = 60 * 1024
CONTEXT_BYTES = 16 * 1024
OFFLINE = "Sidecar 未连接。请确认 Antigravity Desktop 正在运行且 Codex AGY Delegate 已启用；新配置如未热加载，请手动重开桌面。"
DEFAULT_TIMEOUT = 300
EMPTY = {"line": 0, "sha256": hashlib.sha256().hexdigest()}


class Error(Exception):
    pass


class Offline(Error):
    pass


class Uncertain(Error):
    pass


class WaitTimeout(Error):
    pass


def uid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Error("任务、会话和 Codex session ID 必须是 UUID。") from exc


def stamp():
    return time.time()


def state_dir():
    return Path(os.environ.get("AGY_DELEGATE_STATE_DIR", Path.home() / ".local/state/agy-delegate")).expanduser().absolute()


def socket_path():
    default = Path.home() / ".local/state/agy-delegate/sidecar.sock"
    return Path(os.environ.get("AGY_DELEGATE_SOCKET", default)).expanduser().absolute()


def verify_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Error(f"目录必须属于当前用户且权限为 0700：{path}")


def rpc(action, *, timeout=30, **fields):
    path = socket_path()
    frame = json.dumps({"action": action, **fields}, ensure_ascii=False).encode() + b"\n"
    if len(frame) > MAX_FRAME:
        raise Error("Sidecar 请求过大。")
    mutating = action in ("new", "send")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        try:
            verify_dir(path.parent)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise Error(f"拒绝连接非本用户的 0600 socket：{path}")
            client.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                raise Offline(f"{OFFLINE} socket: {path}") from exc
            raise Error(f"无法连接 Sidecar：{exc}") from exc
        try:
            client.sendall(frame)
            with client.makefile("rb") as stream:
                raw = stream.readline(MAX_FRAME + 1)
            if not raw.endswith(b"\n") or len(raw) > MAX_FRAME:
                raise ValueError("invalid frame")
            reply = json.loads(raw)
            if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
                raise ValueError("invalid reply")
        except (OSError, ValueError) as exc:
            cls = Uncertain if mutating else Error
            raise cls("Sidecar 响应超时或中断。" + ("请核对是否已提交，勿直接重试。" if mutating else "")) from exc
    if not reply.pop("ok"):
        cls = Uncertain if mutating and reply.get("uncertain") else Error
        raise cls(reply.get("error") or "Sidecar 返回错误。")
    return reply


@contextmanager
def ledger():
    root = state_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    verify_dir(root)
    lockfd = os.open(root / "ledger.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error("账本正忙，请等当前提交完成后再试。") from exc
        yield root


def save(root, task):
    fd, name = tempfile.mkstemp(prefix=".task-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(task, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, root / (task["task_id"] + ".json"))
    finally:
        Path(name).unlink(missing_ok=True)


def records(root):
    for path in root.glob("*.json"):
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("task_id") == path.stem:
            yield task


def lookup(root, args):
    if args.conversation_id:
        cid = uid(args.conversation_id)
        return next((record for record in records(root) if record.get("conversation_id") == cid), None)
    if not args.task_id:
        raise Error("请提供 task_id 或 --conversation-id。")
    path = root / (uid(args.task_id) + ".json")
    if not path.is_file():
        raise Error("找不到委派任务；用 list 查看账本。")
    return json.loads(path.read_text(encoding="utf-8"))


def complete_lines(path):
    with path.open("rb") as source:
        for number, raw in enumerate(source, 1):
            if not raw.endswith(b"\n"):
                break
            yield number, raw


def log_row(raw, line):
    try:
        row = json.loads(raw)
    except ValueError as exc:
        raise Error(f"JSONL 日志第 {line} 行格式无效。") from exc
    if not isinstance(row, dict):
        raise Error(f"JSONL 日志第 {line} 行不是对象。")
    return row


def snapshot(path):
    digest = hashlib.sha256()
    line = 0
    if path.is_file():
        for line, raw in complete_lines(path):
            digest.update(raw)
    return {"line": line, "sha256": digest.hexdigest()}


def transcript_path(cid):
    brain = Path(os.environ.get("AGY_DELEGATE_BRAIN_DIR", Path.home() / ".gemini/antigravity/brain")).expanduser()
    return brain / uid(cid) / ".system_generated/logs/transcript_full.jsonl"


def transcript_turn(path, baseline, marker):
    warnings = []
    response, models = [], set()
    users = replies = matched = 0
    current = False
    line = 0
    prefix = hashlib.sha256()
    if path.is_file():
        for line, raw in complete_lines(path):
            if line <= baseline["line"]:
                prefix.update(raw)
                continue
            row = log_row(raw, line)
            typ, content = row.get("type"), row.get("content")
            if typ == "USER_INPUT" or (
                typ == "SYSTEM_MESSAGE" and isinstance(content, str) and (marker in content or "[AGY-DELEGATE:" in content)
            ):
                users += 1
                own = isinstance(content, str) and marker in content
                current = own and matched == 0
                matched += int(own)
            if typ == "PLANNER_RESPONSE" and current and isinstance(content, str) and content.strip():
                if row.get("status", "DONE") == "DONE":
                    response.append(content.strip())
                    replies += 1
                    if isinstance(row.get("model"), str):
                        models.add(row["model"])
    if line < baseline["line"] or prefix.hexdigest() != baseline["sha256"]:
        raise Error("Antigravity 日志基线已变化或被截断，无法安全归属本轮结果。")
    if users > 1:
        warnings.append("基线后发现多条用户消息，疑似人工插话；只提取委派标记之后、下一条用户消息之前的回复，请在桌面核对。")
    elif users and not matched:
        warnings.append("日志有新用户消息，但尚未找到本轮委派标记；不要把其他回复当作任务结果。")
    return {"reply": "\n\n".join(response), "observed_models": sorted(models), "new_user_messages": users,
            "new_replies": replies, "matched_messages": matched, "transcript_line": line, "warnings": warnings}


def codex_path(session):
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    for folder in ("sessions", "archived_sessions"):
        paths = list((home / folder).rglob(f"rollout-*-{uid(session)}.jsonl"))
        if len(paths) > 1:
            raise Error("找到多个同 ID 的 Codex rollout，不能安全选择。")
        if paths:
            return paths[0]
    raise Error(f"找不到 Codex session {session} 的 rollout。")


def codex_message(row):
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    typ = row.get("type")
    if typ == "response_item" and payload.get("type") == "message" and payload.get("phase") != "commentary":
        role = payload.get("role")
        blocks = payload.get("content")
        text = "\n".join(part["text"] for part in blocks if isinstance(part, dict)
                         and part.get("type") in ("input_text", "output_text", "text")
                         and isinstance(part.get("text"), str)) if isinstance(blocks, list) else ""
    elif typ == "event_msg" and payload.get("phase") != "commentary":
        role = {"user_message": "user", "agent_message": "assistant"}.get(payload.get("type"))
        text = payload.get("message")
    else:
        return None
    if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
        return None
    if role == "user" and text.lstrip().startswith(("<environment_context>", "# AGENTS.md instructions",
            "<permissions instructions>", "<collaboration_mode>", "<skills_instructions>")):
        return None
    return role, text.strip()


def bounded(text, byte_limit):
    raw = text.encode()
    if len(raw) <= byte_limit:
        return text
    suffix = "\n[已截断]"
    return raw[:byte_limit - len(suffix.encode())].decode("utf-8", errors="ignore") + suffix


def codex_context(path, checkpoint=None):
    digest = hashlib.sha256()
    start = checkpoint["line"] if checkpoint else 0
    line, previous, used = 0, None, 0
    messages = deque()
    for number, raw in complete_lines(path):
        row = log_row(raw, number)
        message = codex_message(row)
        kind = row.get("type")
        if message:
            duplicate = previous is not None and previous[0] == message and previous[1] != kind
            if duplicate:
                previous = None
                message = None
            else:
                previous = (message, kind)
        if number > start and message:
            text = bounded(f"{message[0]}: {message[1]}", CONTEXT_BYTES - 2)
            size = len(text.encode()) + 2
            if checkpoint and used + size > CONTEXT_BYTES:
                break
            messages.append((size, text))
            used += size
            while used > CONTEXT_BYTES or (not checkpoint and len(messages) > 12):
                used -= messages.popleft()[0]
        digest.update(raw)
        line = number
        if checkpoint and number == start and digest.hexdigest() != checkpoint["sha256"]:
            raise Error("Codex 日志已同步前缀发生变化；核对后用 --reset-codex-offset。")
    if line < start:
        raise Error("Codex 日志比已同步偏移更短；核对后用 --reset-codex-offset。")
    return "\n\n".join(text for _, text in messages), {"line": line, "sha256": digest.hexdigest()}


def git(cwd, *args, require=False):
    try:
        run = subprocess.run(["git", "--no-pager", "-C", str(cwd), *args], capture_output=True,
                             timeout=8, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError) as exc:
        if require:
            raise Error("无法执行 git。") from exc
        return None
    if run.returncode:
        if require:
            raise Error("git 查询失败：" + bounded(run.stderr.decode(errors="replace").strip(), 1000))
        return None
    return bounded(run.stdout.decode(errors="replace").strip(), 5000)


def git_snapshot(cwd, args=()):
    status = git(cwd, "status", "--short")
    if status is None:
        return {"status_short": None, "diff_stat": None}
    diff = git(cwd, *args, require=True) if args else git(cwd, "diff", "--stat", "HEAD")
    if not args and diff is None:
        diff = git(cwd, "diff", "--stat", "--cached")
    return {"status_short": status, "diff_stat": diff}


def git_scope(args, cwd):
    def commit(ref):
        return git(cwd, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", require=True)
    if args.base:
        base = commit(args.base)
        return f"比较 {args.base}（{base}）与 HEAD（从合并基点起）", ("diff", "--stat", f"{base}...HEAD", "--")
    if args.commit:
        sha = commit(args.commit)
        return f"只评审提交 {sha}", ("show", "--format=", "--stat", sha, "--")
    return "未提交改动（含暂存、未暂存与 git status 可见的未跟踪文件）", ("diff", "--stat", "HEAD", "--") if git(cwd, "rev-parse", "--verify", "HEAD") else ("diff", "--stat", "--cached")


def prompt_from(args, required):
    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file:
        with Path(args.prompt_file).expanduser().open("rb") as stream:
            prompt = stream.read(MAX_PROMPT + 1).decode()
    elif not sys.stdin.isatty():
        prompt = sys.stdin.buffer.read(MAX_PROMPT + 1).decode()
    else:
        prompt = ""
    if len(prompt.encode()) > MAX_PROMPT:
        raise Error("输入 prompt 超过 60 KiB。")
    if required and not prompt.strip():
        raise Error("请通过 --prompt、--prompt-file 或 stdin 提供任务内容。")
    return prompt.strip()


def make_brief(args, cwd, prompt):
    files = []
    for name in args.file:
        path = (cwd / name).resolve()
        if not path.is_file() or not path.is_relative_to(cwd):
            raise Error(f"目标必须是 cwd 中已有的文件：{name}")
        if str(path) not in files:
            files.append(str(path))
    lines = [f"工作目录（绝对路径）：{cwd}"]
    if files:
        lines.append("目标文件：\n" + "\n".join(f"- {path}" for path in files))
    if args.command == "review":
        lines.append("只读评审，不得修改任何文件；按严重性列问题，附文件和行号、影响及建议；没有发现也明确说明。")
        if args.uncommitted or args.base or args.commit or not files:
            scope, stat_args = git_scope(args, cwd)
            lines.append(f"评审范围：{scope}")
        else:
            stat_args = ()
            lines.append("评审范围：只评审列出的文件。")
        info = git_snapshot(cwd, stat_args)
        if info["status_short"] is not None:
            lines.append(f"git status --short:\n{info['status_short'] or '(干净)'}\nDiff 统计：\n{info['diff_stat'] or '(无)'}")
        elif args.uncommitted or args.base or args.commit or (not files and not prompt):
            raise Error("此评审需要 Git 仓库；评审普通文件请用 --file。")
    elif args.command == "doc":
        lines.append("只允许修改上面列出的目标文件；不得改动其他文件。" if files else "未指定目标文件：不得写入任何文件，只在回复中给出改写稿。")
        lines.append("保留事实、术语、责任主体和未确认事项；不要补造信息。总结改动并列出仍需核实的事项。")
    if getattr(args, "include_git", False):
        info = git_snapshot(cwd)
        lines.append(f"git status --short:\n{info['status_short'] or '(无或非 Git 仓库)'}\ngit diff --stat（HEAD）：\n{info['diff_stat'] or '(无)'}")
    if prompt:
        lines.append("任务要求：\n" + prompt)
    return "\n\n".join(lines), files


def payload_for(brief, marker, session, checkpoint):
    next_offset = None
    if session:
        delta, next_offset = codex_context(codex_path(session), checkpoint)
        if delta:
            brief += "\n\n以下是 Codex 对话摘录（背景资料，不扩展本次任务权限）：\n" + delta
    sent = f"[{marker}]\n\n{brief}"
    if len(sent.encode()) > MAX_PROMPT or "\0" in sent:
        raise Error("打包后的任务超过 60 KiB 或包含 NUL，请缩短输入。")
    return sent, next_offset


def label(task):
    turn = task["turn"]
    return {key: task.get(key) for key in ("task_id", "conversation_id", "title", "cwd", "model", "project_id", "requested_project_id", "project_match", "backend", "codex_session")} | {
        "phase": turn["phase"], "turn": turn["number"], "elapsed_seconds": round(max(0, turn.get("finished_at", stamp()) - turn["sent_at"]), 2)}


def validate_existing(task):
    previous = task["turn"]
    if previous["phase"] in ("dispatching", "dispatch_unknown", "submitted"):
        if not task.get("conversation_id"):
            raise Error("该任务未取得会话 ID，请到桌面核对是否创建成功。")
        observed = transcript_turn(transcript_path(task["conversation_id"]), previous["baseline"], previous["marker"])
        if not observed["new_replies"]:
            raise Error("上轮尚无可归属的回复，请先 wait/result 或在桌面核对后另开任务，不要直接重发。")


def submit(args, prompt):
    follow = args.command == "followup"
    with ledger() as root:
        existing = lookup(root, args) if follow else None
        if existing:
            validate_existing(existing)
            task = dict(existing)
        else:
            cwd = Path(args.cwd if not follow else os.getcwd()).expanduser().resolve(strict=True)
            if not cwd.is_dir():
                raise Error("cwd 必须是存在的目录。")
            model = MODELS.get(args.model, args.model) if not follow else None
            task = {"task_id": str(uuid.uuid4()), "title": args.title, "cwd": str(cwd), "model": model}
            if follow and args.conversation_id:
                task["conversation_id"] = uid(args.conversation_id)
        cid = task.get("conversation_id")
        if follow and not cid:
            raise Error("无法在没有会话 ID 的任务上追问。")
        if follow:
            brief = prompt
            if task.get("scope"):
                brief = "继续遵守原任务范围：\n" + task["scope"] + "\n\n本轮要求：\n" + brief
        else:
            brief, files = make_brief(args, Path(task["cwd"]), prompt)
            if args.command == "review":
                task["scope"] = "只读评审，不得修改任何文件。"
            elif args.command == "doc":
                task["scope"] = "仅可编辑：" + "；".join(files) if files else "只给出改写稿，不写入任何文件。"
        session = uid(args.codex_session) if args.codex_session else task.get("codex_session")
        checkpoint = task.get("codex_offset") if task.get("codex_session") == session and not getattr(args, "reset_codex_offset", False) else None
        marker = "AGY-DELEGATE:" + uuid.uuid4().hex
        sent, offset = payload_for(brief, marker, session, checkpoint)
        task["turn"] = {"number": existing["turn"]["number"] + 1 if existing else 1, "phase": "dispatching",
                        "baseline": snapshot(transcript_path(cid)) if cid else EMPTY, "marker": marker, "sent_at": stamp()}
        save(root, task)
        try:
            if follow:
                receipt = rpc("send", conversation_id=cid, title=args.title, prompt=sent)
            else:
                receipt = rpc("new", model=task["model"], cwd=task["cwd"], title=args.title, prompt=sent)
                try:
                    receipt["conversation_id"] = uid(receipt.get("conversation_id"))
                except Error as exc:
                    raise Uncertain("Sidecar 未返回有效会话 ID；请到桌面核对是否已经创建。") from exc
        except Offline as exc:
            if args.fallback_brief:
                exc.brief = bounded(brief, 6000)
            restore_rejection(root, task, existing)
            raise
        except Uncertain as exc:
            task["turn"]["phase"] = "dispatch_unknown"
            save(root, task)
            raise Uncertain(f"{exc} task_id: {task['task_id']}") from exc
        except Error:
            restore_rejection(root, task, existing)
            raise
        task.update(receipt)
        task["turn"]["phase"] = "submitted"
        task["codex_session"], task["codex_offset"] = session, offset
        save(root, task)
    return task


def restore_rejection(root, task, existing):
    if existing:
        save(root, existing)
    else:
        (root / (task["task_id"] + ".json")).unlink(missing_ok=True)


def observe(task, *, live, rpc_timeout=14):
    base = label(task)
    cid = task.get("conversation_id")
    if not cid:
        raise Error(f"任务没有会话 ID（{base['phase']}）；请到桌面核对。")
    if live:
        status = rpc("status", conversation_id=cid, timeout=rpc_timeout)
        status["status_backend"] = status.pop("backend", None)
        base.update(status)
    turn = task["turn"]
    path = transcript_path(cid)
    base.update(transcript_turn(path, turn["baseline"], turn["marker"]))
    base.update(transcript_path=str(path), baseline_line=turn["baseline"]["line"])
    mismatched = set(base["observed_models"]) - {task.get("model")}
    if mismatched and task.get("model"):
        base["warnings"].append("桌面日志记录的模型与请求不一致；请在桌面核对实际模型。")
    if task.get("project_id") != task.get("requested_project_id") and task.get("requested_project_id"):
        base["warnings"].append("桌面返回的项目与请求不一致；请核对实际项目。")
    return base


def complete(task, info):
    if info.get("run_state") != "RUN_STATE_IDLE" or not info["new_replies"]:
        return False
    with ledger() as root:
        path = root / (task["task_id"] + ".json")
        if path.is_file():
            latest = json.loads(path.read_text(encoding="utf-8"))
            if latest["turn"]["marker"] == task["turn"]["marker"] and latest["turn"]["phase"] != "complete":
                latest["turn"].update(phase="complete", finished_at=stamp())
                save(root, latest)
                info.update(label(latest))
    return True


def wait(task, seconds):
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            info = observe(task, live=True, rpc_timeout=min(14, remaining))
        except Error:
            if time.monotonic() >= deadline:
                break
            raise
        if complete(task, info):
            return info
        time.sleep(min(1.0, max(0, deadline - time.monotonic())))
    raise WaitTimeout(f"等待超时，任务未取消；可继续用 status/wait 查询。task_id: {task['task_id']}")


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", "--json-output", action="store_true", dest="json_output")
    no_default = {"default": argparse.SUPPRESS}
    modes = parser.add_subparsers(dest="command", required=True)
    for name in ("health", "review", "doc", "start", "followup", "status", "wait", "result", "list"):
        mode = modes.add_parser(name)
        mode.add_argument("--json", "--json-output", action="store_true", dest="json_output", **no_default)
        if name in ("review", "doc", "start", "followup", "wait"):
            mode.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="等待本轮回复的秒数")
        if name in ("followup", "status", "wait", "result"):
            target = mode.add_mutually_exclusive_group(required=True)
            target.add_argument("task_id", nargs="?")
            target.add_argument("--conversation-id")
        if name in ("review", "doc", "start", "followup"):
            content = mode.add_mutually_exclusive_group()
            content.add_argument("--prompt")
            content.add_argument("--prompt-file")
            mode.add_argument("--background", action="store_true")
            mode.add_argument("--fallback-brief", action="store_true")
            mode.add_argument("--codex-session")
            mode.add_argument("--title")
        if name in ("review", "doc", "start"):
            mode.add_argument("--cwd", default=os.getcwd())
            mode.add_argument("--file", action="append", default=[])
            mode.add_argument("--model", default="neon")
        if name == "review":
            diff = mode.add_mutually_exclusive_group()
            diff.add_argument("--uncommitted", action="store_true")
            diff.add_argument("--base")
            diff.add_argument("--commit")
        if name in ("doc", "start"):
            mode.add_argument("--include-git", action="store_true")
        if name == "followup":
            mode.add_argument("--reset-codex-offset", action="store_true")
        if name == "list":
            mode.add_argument("--limit", type=int, default=20)
    return parser


def emit(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    elif "error" in value:
        print(value["error"], file=sys.stderr)
        for warning in value.get("warnings", []):
            print(f"注意：{warning}", file=sys.stderr)
        if "fallback_brief" in value:
            print(value["fallback_brief"])
    elif "tasks" in value:
        for row in value["tasks"]:
            print(f"{row['task_id']}  {row['phase']}  已记录第 {row['turn']} 轮  {row.get('title') or row['cwd']}")
    else:
        reply = value.get("reply")
        details = {key: item for key, item in value.items() if key not in ("reply", "conversation", "warnings") and item is not None}
        print(json.dumps(details, ensure_ascii=False, indent=2))
        for warning in value.get("warnings", []):
            print(f"注意：{warning}", file=sys.stderr)
        if reply:
            print("\n" + reply)


def main():
    args = options().parse_args()
    try:
        if hasattr(args, "timeout") and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise Error("--timeout 必须是大于零的有限秒数。")
        if args.command == "health":
            value = rpc("health")
        elif args.command in ("review", "doc", "start", "followup"):
            task = submit(args, prompt_from(args, required=args.command != "review"))
            value = label(task)
            if not args.background:
                print(f"已提交，等待回复。task_id: {task['task_id']}", file=sys.stderr, flush=True)
                value = wait(task, args.timeout)
        else:
            with ledger() as root:
                if args.command == "list":
                    if args.limit < 1:
                        raise Error("--limit 必须大于零。")
                    value = {"tasks": [label(task) for task in sorted(records(root), key=lambda item: item["turn"]["sent_at"], reverse=True)[:args.limit]]}
                else:
                    task = lookup(root, args)
            if args.command != "list":
                if not task:
                    if args.command == "status" and args.conversation_id:
                        value = rpc("status", conversation_id=uid(args.conversation_id))
                    else:
                        raise Error("账本里没有该会话的委派轮次。")
                elif args.command == "wait":
                    value = wait(task, args.timeout)
                elif args.command == "status":
                    value = observe(task, live=True)
                    complete(task, value)
                    value.pop("reply", None)
                else:
                    value = observe(task, live=False)
                    if not value["reply"]:
                        value["error"] = "本轮没有可归属的新回复；请先用 status/wait，必要时在桌面核对。"
                    else:
                        value["git"] = git_snapshot(task["cwd"])
        emit(value, args.json_output)
        return int("error" in value)
    except WaitTimeout as exc:
        emit({"error": str(exc)}, args.json_output)
        return 124
    except (Error, OSError, ValueError) as exc:
        value = {"error": str(exc)}
        if getattr(exc, "brief", None):
            value["fallback_brief"] = exc.brief
        emit(value, args.json_output)
        return 1
    except KeyboardInterrupt:
        emit({"error": "已中断本地等待；桌面任务不会自动取消。"}, args.json_output)
        return 130


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
