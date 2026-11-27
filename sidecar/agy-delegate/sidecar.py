#!/usr/bin/env python3
"""Antigravity Desktop command sidecar; no public or TCP listener."""

import datetime
import fcntl
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit
import uuid


MAX_FRAME = 256 * 1024
MAX_PROMPT = 60 * 1024
BACKEND_TIMEOUT = 12
CONVERSATIONS = "/product.v1.ConversationService/"
MODELS = "/exa.language_server_pb.LanguageServerService/GetAvailableModels"
STATES = {"RUN_STATE_IDLE", "RUN_STATE_RUNNING", "RUN_STATE_CANCELLING"}


class Rejected(Exception):
    pass


class Uncertain(Exception):
    pass


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Rejected(f"The directory must be owned by the current user and have mode 0700: {path}")


def project_for(cwd, projects, default):
    if not cwd:
        return default, "default"
    target = Path(cwd).resolve(strict=True)
    if not target.is_dir():
        raise Rejected("cwd must be an existing directory.")
    matches = []
    for path in Path(projects).glob("*.json"):
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
            project_id = project["id"]
            if not isinstance(project_id, str) or not project_id or project.get("sidecar"):
                continue
            resources = project.get("projectResources", {}).get("resources", [])
            for resource in resources:
                for value in resource.values():
                    uri = value.get("folderUri") if isinstance(value, dict) else None
                    if not isinstance(uri, str):
                        continue
                    parsed = urlsplit(uri)
                    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                        continue
                    try:
                        folder = Path(unquote(parsed.path)).resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if folder == target or folder in target.parents:
                        matches.append((len(folder.parts), project_id))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if not matches:
        return default, "unmatched_default"
    depth = max(item[0] for item in matches)
    nearest = {project_id for size, project_id in matches if size == depth}
    if len(nearest) != 1:
        raise Rejected("Multiple Antigravity projects match cwd. Resolve the duplicate project entries in Desktop first.")
    return nearest.pop(), "matched"


def field(request, key, limit, optional=False):
    value = request.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value.encode()) > limit:
        raise Rejected(f"{key} must be non-empty text of at most {limit} bytes.")
    return value


def conversation_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Rejected("conversation_id must be a UUID.") from exc


class Backend:
    def __init__(self):
        self.started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.address = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        self.csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
        self.project = os.environ.get("ANTIGRAVITY_PROJECT_ID")
        self.projects = os.environ.get("AGY_DELEGATE_PROJECTS_DIR", Path.home() / ".gemini/config/projects")
        exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
        shim = shutil.which("agentapi") or shutil.which(str(Path.home() / ".gemini/antigravity/bin/agentapi"))
        self.agentapi = [exe, "agentapi"] if exe and os.access(exe, os.X_OK) else [shim] if shim else []
        self.locks = [threading.Lock() for _ in range(32)]

    def rpc(self, route, body, *, mutating=False):
        if not self.address or not self.csrf:
            raise Rejected("The Desktop Sidecar environment is missing the language server address or credentials.")
        address = urlsplit("http://" + self.address)
        if address.hostname not in ("127.0.0.1", "localhost", "::1") or not address.port or address.path:
            raise Rejected("The language server must use a local loopback address.")
        client = http.client.HTTPConnection(address.hostname, address.port, timeout=BACKEND_TIMEOUT)
        try:
            client.request("POST", route, json.dumps(body), {
                "Content-Type": "application/json", "Connect-Protocol-Version": "1",
                "x-antigravity-csrf-token": self.csrf,
            })
            response = client.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200:
                error = f"Desktop API {route.rsplit('/', 1)[-1]} returned HTTP {response.status}."
                if not mutating or response.status in (400, 401, 403, 404, 422):
                    raise Rejected(error)
                raise Uncertain(error)
            data = json.loads(raw)
            if len(raw) > 4 * 1024 * 1024 or not isinstance(data, dict):
                raise ValueError("invalid response")
            return data
        except (OSError, ValueError, http.client.HTTPException) as exc:
            error = Uncertain if mutating else Rejected
            raise error(f"Desktop API {route.rsplit('/', 1)[-1]} did not return a valid response.") from exc
        finally:
            client.close()

    def status(self, cid):
        data = self.rpc(CONVERSATIONS + "GetConversation", {"conversationId": cid})
        conv = data.get("conversation", data)
        state = conv.get("state", {}).get("runState")
        if state not in STATES or conv.get("id") != cid:
            raise Rejected("Desktop did not return a valid runState for this conversation.")
        return {"conversation_id": cid, "run_state": state, "conversation": conv,
                "backend": "ConversationService/GetConversation"}

    def dispatch(self, request):
        action = request.get("action")
        if action == "health":
            ready = bool(self.address and self.csrf and self.agentapi)
            return {"state": "ready" if ready else "degraded", "pid": os.getpid(), "started_at": self.started,
                    "agentapi_path": self.agentapi[0] if self.agentapi else None,
                    "has_ls_address": bool(self.address), "has_csrf_token": bool(self.csrf),
                    "default_project_id": self.project}
        if action == "new":
            prompt = field(request, "prompt", MAX_PROMPT)
            model = field(request, "model", 256)
            title = field(request, "title", 512, optional=True)
            cwd = field(request, "cwd", 4096, optional=True)
            if not self.agentapi:
                raise Rejected("The Sidecar could not find agentapi and cannot follow up on a new conversation.")
            project, match = project_for(cwd, self.projects, self.project)
            catalog = self.rpc(MODELS, {}).get("response", {})
            available = {item for sort in catalog.get("agentModelSorts", [])
                         for group in sort.get("groups", []) for item in group.get("modelIds", [])}
            if model not in available or model not in catalog.get("models", {}):
                raise Rejected(f"Desktop does not list {model} as an available conversation model. No other model was used.")
            payload = {"model": model, "prompt": [{"text": prompt}]}
            if title:
                payload["title"] = title
            if project:
                payload["projectId"] = project
            data = self.rpc(CONVERSATIONS + "CreateConversation", payload, mutating=True)
            conv = data.get("conversation", data)
            try:
                cid = conversation_id(conv.get("id"))
            except (AttributeError, Rejected) as exc:
                raise Uncertain("Desktop did not return a valid conversation ID. Check in Desktop whether the conversation was created.") from exc
            actual_project = conv.get("metadata", {}).get("projectId")
            return {"conversation_id": cid, "project_id": actual_project, "requested_project_id": project,
                    "project_match": match, "model": model, "backend": "ConversationService/CreateConversation"}
        if action == "status":
            return self.status(conversation_id(request.get("conversation_id")))
        if action == "send":
            cid = conversation_id(request.get("conversation_id"))
            prompt = field(request, "prompt", MAX_PROMPT)
            title = field(request, "title", 512, optional=True)
            if not self.agentapi:
                raise Rejected("The Sidecar could not find agentapi.")
            lock = self.locks[uuid.UUID(cid).int % len(self.locks)]
            if not lock.acquire(blocking=False):
                raise Rejected("Another submission is in progress for this conversation. Check its status again later.")
            try:
                status = self.status(cid)
                if status["run_state"] != "RUN_STATE_IDLE":
                    raise Rejected("The conversation is not idle. Wait for the current turn to finish or click Stop in Desktop.")
                cmd = self.agentapi + ["send-message"] + ([f"--title={title}"] if title else []) + [cid, prompt]
                env = os.environ.copy()
                project = status["conversation"].get("metadata", {}).get("projectId")
                if project:
                    env["ANTIGRAVITY_PROJECT_ID"] = project
                else:
                    env.pop("ANTIGRAVITY_PROJECT_ID", None)
                try:
                    completed = subprocess.run(cmd, check=True, capture_output=True, timeout=BACKEND_TIMEOUT, env=env)
                    receipt = json.loads(completed.stdout) if completed.stdout.strip() else {}
                    if not isinstance(receipt, dict) or receipt.get("error"):
                        raise ValueError("invalid receipt")
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    raise Uncertain("agentapi did not confirm delivery. Check Desktop or the local logs before resending.") from exc
                return {"conversation_id": cid, "project_id": project, "backend": "agentapi/send-message"}
            finally:
                lock.release()
        raise Rejected("Unknown Sidecar action.")


class Handler(socketserver.StreamRequestHandler):
    timeout = 4

    def handle(self):
        try:
            raw = self.rfile.readline(MAX_FRAME + 1)
            if not raw or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                raise Rejected("Expected a single-line JSON request of at most 256 KiB.")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise Rejected("The request must be a JSON object.")
            response = {"ok": True, **self.server.backend.dispatch(request)}
        except (json.JSONDecodeError, UnicodeError, TimeoutError):
            response = {"ok": False, "error": "Invalid request format or read timeout."}
        except Rejected as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            if not isinstance(exc, Uncertain):
                print(f"Sidecar request failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            response = {"ok": False, "uncertain": True,
                        "error": str(exc) if isinstance(exc, Uncertain) else "The Sidecar could not confirm the outcome."}
        try:
            data = json.dumps(response, ensure_ascii=False).encode() + b"\n"
            self.wfile.write(data if len(data) <= MAX_FRAME else b'{"ok":false,"uncertain":true,"error":"Response too large"}\n')
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    timeout = 0.2


def serve(path):
    private_dir(path.parent)
    lockfd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise Rejected(f"Refusing to replace a path that is not a socket owned by the current user: {path}")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise Rejected(f"A Sidecar is already listening at {path}")
        old_mask = os.umask(0o177)
        try:
            server = Server(str(path), Handler)
        finally:
            os.umask(old_mask)
        try:
            path.chmod(0o600)
            server.backend = Backend()
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            print(f"agy-delegate listening on {path}", flush=True)
            while not stop.is_set():
                server.handle_request()
        finally:
            server.server_close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    os.umask(0o077)
    default_path = Path.home() / ".local/state/agy-delegate/sidecar.sock"
    try:
        serve(Path(os.environ.get("AGY_DELEGATE_SOCKET", default_path)).expanduser().absolute())
    except (OSError, Rejected) as exc:
        print(f"agy-delegate: {exc}", file=sys.stderr)
        sys.exit(1)
