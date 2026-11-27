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
OFFLINE = ("Cannot connect to the Sidecar. Make sure Antigravity Desktop is running and Codex AGY Delegate is enabled. "
           "If Desktop did not pick up the new configuration, restart it manually.")
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
        raise Error("Task, conversation, and Codex session IDs must be UUIDs.") from exc


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
        raise Error(f"The directory must be owned by the current user and have mode 0700: {path}")


def rpc(action, *, timeout=30, **fields):
    path = socket_path()
    frame = json.dumps({"action": action, **fields}, ensure_ascii=False).encode() + b"\n"
    if len(frame) > MAX_FRAME:
        raise Error("Sidecar request too large.")
    mutating = action in ("new", "send")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        try:
            verify_dir(path.parent)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise Error(f"Refusing to connect: expected a socket owned by the current user with mode 0600: {path}")
            client.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                raise Offline(f"{OFFLINE} socket: {path}") from exc
            raise Error(f"Could not connect to the Sidecar: {exc}") from exc
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
            raise cls("The Sidecar response timed out or was interrupted."
                      + (" Verify whether the request was submitted before retrying." if mutating else "")) from exc
    if not reply.pop("ok"):
        cls = Uncertain if mutating and reply.get("uncertain") else Error
        raise cls(reply.get("error") or "The Sidecar returned an error.")
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
            raise Error("The ledger is busy. Wait for the current submission to finish before retrying.") from exc
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
        raise Error("Provide task_id or --conversation-id.")
    path = root / (uid(args.task_id) + ".json")
    if not path.is_file():
        raise Error("No delegated task found. Use list to inspect the ledger.")
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
        raise Error(f"Invalid JSON on log line {line}.") from exc
    if not isinstance(row, dict):
        raise Error(f"JSONL log line {line} is not an object.")
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
        raise Error("The Antigravity log baseline changed or was truncated. Cannot safely attribute replies to this turn.")
    if users > 1:
        warnings.append("Multiple user messages appeared after the baseline, possibly from manual input. "
                        "Only replies between the delegation marker and the next user message are included. Verify in Desktop.")
    elif users and not matched:
        warnings.append("The log contains new user messages, but this turn's delegation marker has not appeared. "
                        "Do not treat other replies as this task's result.")
    return {"reply": "\n\n".join(response), "observed_models": sorted(models), "new_user_messages": users,
            "new_replies": replies, "matched_messages": matched, "transcript_line": line, "warnings": warnings}


def codex_path(session):
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    for folder in ("sessions", "archived_sessions"):
        paths = list((home / folder).rglob(f"rollout-*-{uid(session)}.jsonl"))
        if len(paths) > 1:
            raise Error("Multiple Codex rollouts match this ID. Cannot safely select one.")
        if paths:
            return paths[0]
    raise Error(f"No rollout found for Codex session {session}.")


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
    suffix = "\n[Truncated]"
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
            raise Error("The previously synced Codex log prefix has changed. Verify the log before using --reset-codex-offset.")
    if line < start:
        raise Error("The Codex log ends before the saved sync offset. Verify the log before using --reset-codex-offset.")
    return "\n\n".join(text for _, text in messages), {"line": line, "sha256": digest.hexdigest()}


def git(cwd, *args, require=False):
    try:
        run = subprocess.run(["git", "--no-pager", "-C", str(cwd), *args], capture_output=True,
                             timeout=8, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError) as exc:
        if require:
            raise Error("Could not run Git.") from exc
        return None
    if run.returncode:
        if require:
            raise Error("Git query failed: " + bounded(run.stderr.decode(errors="replace").strip(), 1000))
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
        return f"Compare HEAD with {args.base} ({base}), starting at their merge base", ("diff", "--stat", f"{base}...HEAD", "--")
    if args.commit:
        sha = commit(args.commit)
        return f"Review only commit {sha}", ("show", "--format=", "--stat", sha, "--")
    return "Uncommitted changes (staged and unstaged changes, plus untracked files shown by git status)", ("diff", "--stat", "HEAD", "--") if git(cwd, "rev-parse", "--verify", "HEAD") else ("diff", "--stat", "--cached")


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
        raise Error("The prompt exceeds 60 KiB.")
    if required and not prompt.strip():
        raise Error("Provide a task prompt via --prompt, --prompt-file, or stdin.")
    return prompt.strip()


def make_brief(args, cwd, prompt):
    files = []
    for name in args.file:
        path = (cwd / name).resolve()
        if not path.is_file() or not path.is_relative_to(cwd):
            raise Error(f"The target must be an existing file within cwd: {name}")
        if str(path) not in files:
            files.append(str(path))
    lines = [f"Working directory (absolute path): {cwd}"]
    if files:
        lines.append("Target files:\n" + "\n".join(f"- {path}" for path in files))
    if args.command == "review":
        lines.append("Read-only review: do not modify any files. List findings by severity, with file paths, "
                     "line numbers, impact, and recommendations. If there are no findings, say so.")
        if args.uncommitted or args.base or args.commit or not files:
            scope, stat_args = git_scope(args, cwd)
            lines.append(f"Review scope: {scope}")
        else:
            stat_args = ()
            lines.append("Review scope: review only the listed files.")
        info = git_snapshot(cwd, stat_args)
        if info["status_short"] is not None:
            lines.append(f"git status --short:\n{info['status_short'] or '(clean)'}\nDiff stats:\n{info['diff_stat'] or '(none)'}")
        elif args.uncommitted or args.base or args.commit or (not files and not prompt):
            raise Error("This review requires a Git repository. Use --file to review individual files.")
    elif args.command == "doc":
        lines.append("Edit only the target files listed above. Do not modify any other files." if files else
                     "No target files specified. Do not write to any files; provide the revised draft in your reply only.")
        lines.append("Preserve facts, terminology, responsible parties, and unresolved points. Do not invent details. "
                     "Summarize your changes and list anything that still needs verification.")
    if getattr(args, "include_git", False):
        info = git_snapshot(cwd)
        lines.append(f"git status --short:\n{info['status_short'] or '(no changes or not a Git repository)'}\ngit diff --stat (HEAD):\n{info['diff_stat'] or '(none)'}")
    if prompt:
        lines.append("Task requirements:\n" + prompt)
    return "\n\n".join(lines), files


def payload_for(brief, marker, session, checkpoint):
    next_offset = None
    if session:
        delta, next_offset = codex_context(codex_path(session), checkpoint)
        if delta:
            brief += "\n\nCodex transcript excerpt (background only; it does not grant additional permissions):\n" + delta
    sent = f"[{marker}]\n\n{brief}"
    if len(sent.encode()) > MAX_PROMPT or "\0" in sent:
        raise Error("The assembled brief exceeds 60 KiB or contains a NUL character. Shorten the input or remove NUL characters.")
    return sent, next_offset


def label(task):
    turn = task["turn"]
    return {key: task.get(key) for key in ("task_id", "conversation_id", "title", "cwd", "model", "project_id", "requested_project_id", "project_match", "backend", "codex_session")} | {
        "phase": turn["phase"], "turn": turn["number"], "elapsed_seconds": round(max(0, turn.get("finished_at", stamp()) - turn["sent_at"]), 2)}


def validate_existing(task):
    previous = task["turn"]
    if previous["phase"] in ("dispatching", "dispatch_unknown", "submitted"):
        if not task.get("conversation_id"):
            raise Error("This task has no conversation ID. Check in Desktop whether the conversation was created.")
        observed = transcript_turn(transcript_path(task["conversation_id"]), previous["baseline"], previous["marker"])
        if not observed["new_replies"]:
            raise Error("No reply can be attributed to the previous turn yet. Use wait or result, or verify in Desktop "
                        "before starting a separate task. Do not resend without checking.")


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
                raise Error("cwd must be an existing directory.")
            model = MODELS.get(args.model, args.model) if not follow else None
            task = {"task_id": str(uuid.uuid4()), "title": args.title, "cwd": str(cwd), "model": model}
            if follow and args.conversation_id:
                task["conversation_id"] = uid(args.conversation_id)
        cid = task.get("conversation_id")
        if follow and not cid:
            raise Error("Cannot follow up on a task without a conversation ID.")
        if follow:
            brief = prompt
            if task.get("scope"):
                brief = "Keep to the original task scope:\n" + task["scope"] + "\n\nRequirements for this turn:\n" + brief
        else:
            brief, files = make_brief(args, Path(task["cwd"]), prompt)
            if args.command == "review":
                task["scope"] = "Read-only review: do not modify any files."
            elif args.command == "doc":
                task["scope"] = "Edit only: " + "; ".join(files) if files else "Provide the revised draft in your reply only. Do not write to any files."
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
                    raise Uncertain("The Sidecar did not return a valid conversation ID. Check in Desktop whether the conversation was created.") from exc
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
        raise Error(f"The task has no conversation ID (phase: {base['phase']}). Verify in Desktop.")
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
        base["warnings"].append("The model in the Desktop log differs from the requested model. Verify the actual model in Desktop.")
    if task.get("project_id") != task.get("requested_project_id") and task.get("requested_project_id"):
        base["warnings"].append("Desktop returned a different project than requested. Verify which project was used.")
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
    raise WaitTimeout(f"Wait timed out. The task was not canceled. Use status or wait to check again. task_id: {task['task_id']}")


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", "--json-output", action="store_true", dest="json_output")
    no_default = {"default": argparse.SUPPRESS}
    modes = parser.add_subparsers(dest="command", required=True)
    for name in ("health", "review", "doc", "start", "followup", "status", "wait", "result", "list"):
        mode = modes.add_parser(name)
        mode.add_argument("--json", "--json-output", action="store_true", dest="json_output", **no_default)
        if name in ("review", "doc", "start", "followup", "wait"):
            mode.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Seconds to wait for a reply to the current turn")
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
            print(f"Warning: {warning}", file=sys.stderr)
        if "fallback_brief" in value:
            print(value["fallback_brief"])
    elif "tasks" in value:
        for row in value["tasks"]:
            print(f"{row['task_id']}  {row['phase']}  Recorded turn {row['turn']}  {row.get('title') or row['cwd']}")
    else:
        reply = value.get("reply")
        details = {key: item for key, item in value.items() if key not in ("reply", "conversation", "warnings") and item is not None}
        print(json.dumps(details, ensure_ascii=False, indent=2))
        for warning in value.get("warnings", []):
            print(f"Warning: {warning}", file=sys.stderr)
        if reply:
            print("\n" + reply)


def main():
    args = options().parse_args()
    try:
        if hasattr(args, "timeout") and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise Error("--timeout must be a finite, positive number of seconds.")
        if args.command == "health":
            value = rpc("health")
        elif args.command in ("review", "doc", "start", "followup"):
            task = submit(args, prompt_from(args, required=args.command != "review"))
            value = label(task)
            if not args.background:
                print(f"Submitted; waiting for a reply. task_id: {task['task_id']}", file=sys.stderr, flush=True)
                value = wait(task, args.timeout)
        else:
            with ledger() as root:
                if args.command == "list":
                    if args.limit < 1:
                        raise Error("--limit must be greater than zero.")
                    value = {"tasks": [label(task) for task in sorted(records(root), key=lambda item: item["turn"]["sent_at"], reverse=True)[:args.limit]]}
                else:
                    task = lookup(root, args)
            if args.command != "list":
                if not task:
                    if args.command == "status" and args.conversation_id:
                        value = rpc("status", conversation_id=uid(args.conversation_id))
                    else:
                        raise Error("The ledger contains no delegated turns for this conversation.")
                elif args.command == "wait":
                    value = wait(task, args.timeout)
                elif args.command == "status":
                    value = observe(task, live=True)
                    complete(task, value)
                    value.pop("reply", None)
                else:
                    value = observe(task, live=False)
                    if not value["reply"]:
                        value["error"] = "No new reply can be attributed to this turn. Use status or wait, and verify in Desktop if needed."
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
        emit({"error": "Local wait interrupted. The Desktop task will not be canceled automatically."}, args.json_output)
        return 130


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
