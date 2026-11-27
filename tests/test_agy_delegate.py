from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills/agy-delegate/scripts/agy_delegate.py"
SIDECAR = ROOT / "sidecar/agy-delegate/sidecar.py"
SOCKET_TMP = os.environ.get("TMPDIR") or "/tmp"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load(CLI, "agy_delegate")
sidecar = load(SIDECAR, "sidecar")


def append(path, *items):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for item in items:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def user(text):
    return {"type": "USER_INPUT", "status": "DONE", "content": text}


def reply(text, **extra):
    return {"type": "PLANNER_RESPONSE", "status": "DONE", "content": text, **extra}


class Logs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "log.jsonl"

    def test_transcript_ignores_old_partial_and_human_turns(self):
        append(self.file, user("old"), reply("old reply"))
        before = cli.snapshot(self.file)
        append(self.file, user("[marker] our prompt"), {**reply("thinking"), "status": "RUNNING"},
               reply("our reply", model="real-model"), user("A person wrote this"), reply("unrelated"))
        with self.file.open("a") as stream:
            stream.write('{"incomplete":')
        row = cli.transcript_turn(self.file, before, "[marker]")
        self.assertEqual(row["reply"], "our reply")
        self.assertEqual(row["observed_models"], ["real-model"])
        self.assertEqual(row["transcript_line"], 7)
        self.assertEqual(row["new_user_messages"], 2)
        self.assertTrue(row["warnings"])

    def test_transcript_never_uses_unmatched_reply_and_checks_prefix(self):
        append(self.file, user("human"), reply("does not belong to task"))
        self.assertEqual(cli.transcript_turn(self.file, cli.EMPTY, "missing")["reply"], "")
        checkpoint = cli.snapshot(self.file)
        self.file.write_text(self.file.read_text().replace("human", "other"))
        with self.assertRaisesRegex(cli.Error, "log baseline changed or was truncated"):
            cli.transcript_turn(self.file, checkpoint, "missing")

    def test_codex_incremental_hash_and_cross_offset_dedup(self):
        event = {"type": "event_msg", "payload": {"type": "user_message", "message": "Original message: café"}}
        item = {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Original message: café"}]}}
        append(self.file, event)
        original, checkpoint = cli.codex_context(self.file)
        self.assertIn("Original message: café", original)
        append(self.file, item, {"type": "response_item", "payload": {"type": "function_call", "arguments": "do-not-send"}},
               {"type": "event_msg", "payload": {"type": "agent_message", "message": "New message: résumé"}})
        with self.file.open("a") as stream:
            stream.write('{"incomplete":')
        delta, new = cli.codex_context(self.file, checkpoint)
        self.assertEqual(delta, "assistant: New message: résumé")
        self.assertEqual(new, {"line": 4, "sha256": cli.snapshot(self.file)["sha256"]})
        self.file.write_text(self.file.read_text().replace("Original message: café", "Modified message: café", 1))
        with self.assertRaisesRegex(cli.Error, "reset-codex-offset"):
            cli.codex_context(self.file, checkpoint)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server
        method = self.path.rsplit("/", 1)[-1]
        srv.calls.append((method, body))
        code = 200
        if self.headers.get("x-antigravity-csrf-token") != self.server.csrf:
            code, response = 403, {}
        elif method == "GetAvailableModels":
            models = dict.fromkeys(srv.models, {})
            response = {"response": {"models": models, "agentModelSorts": [{"groups": [{"modelIds": list(models)}]}]}}
        elif method == "CreateConversation":
            srv.project = body.get("projectId")
            response = {"conversation": {"id": srv.cid, "metadata": {"projectId": srv.project}}}
        elif method == "GetConversation":
            response = {"id": srv.cid, "metadata": {"projectId": srv.project}, "state": {"runState": srv.run_state}}
        else:
            code, response = 404, {}
        data = json.dumps(response).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=SOCKET_TMP, prefix="agy-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.sock = cls.root / "socket"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.server.cid = str(uuid.uuid4())
        cls.server.csrf = uuid.uuid4().hex
        cls.server.models = [*cli.MODELS.values(), "provider-custom-full-id"]
        cls.server.calls = []
        cls.server.project = "default-project"
        cls.server.run_state = "RUN_STATE_IDLE"
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.repo = cls.root / "café repo"
        cls.work = cls.repo / "nested"
        cls.work.mkdir(parents=True)
        cls.projects = cls.root / "projects"
        cls.projects.mkdir()
        for project, folder in (("outer", cls.repo), ("inner", cls.work)):
            (cls.projects / f"{project}.json").write_text(json.dumps({"id": project, "projectResources": {"resources": [{"gitFolder": {"folderUri": folder.as_uri()}}]}}))
        cls.exec_log = cls.root / "agentapi.jsonl"
        cls.stub_fail = cls.root / "agentapi-fail"
        cls.agentapi = cls.root / "stub-executable"
        cls.agentapi.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
if sys.argv[1:3] != ["agentapi", "send-message"]: sys.exit(3)
if Path(os.environ["FAKE_FAIL"]).exists(): sys.exit(4)
with open(os.environ["FAKE_ARGS"], "a") as stream:
    print(json.dumps([os.environ.get("ANTIGRAVITY_PROJECT_ID"), sys.argv[3:]]), file=stream)
print(json.dumps({{"response": {{}}}}))
''')
        cls.agentapi.chmod(0o700)
        cls.env = {**os.environ, "AGY_DELEGATE_SOCKET": str(cls.sock), "AGY_DELEGATE_PROJECTS_DIR": str(cls.projects),
                   "ANTIGRAVITY_AGENTAPI_EXE": str(cls.agentapi), "ANTIGRAVITY_PROJECT_ID": "default-project",
                   "ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{cls.server.server_port}", "ANTIGRAVITY_CSRF_TOKEN": cls.server.csrf,
                   "FAKE_ARGS": str(cls.exec_log), "FAKE_FAIL": str(cls.stub_fail)}
        cls.proc = subprocess.Popen([sys.executable, SIDECAR], env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.addClassCleanup(cls.stop_sidecar)
        for _ in range(100):
            if cls.sock.exists() and stat.S_IMODE(cls.sock.lstat().st_mode) == 0o600:
                break
            if cls.proc.poll() is not None:
                raise RuntimeError(cls.proc.communicate(timeout=1)[1].decode())
            time.sleep(0.02)
        else:
            raise RuntimeError("Sidecar test socket did not start")

    @classmethod
    def stop_sidecar(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
        cls.proc.communicate(timeout=6)
        if cls.sock.exists():
            raise AssertionError("Sidecar did not clean up its socket on SIGTERM")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(tmp.cleanup)
        temp_path = Path(tmp.name)
        self.state = temp_path / "state"
        self.brain = temp_path / "brain"
        self.codex = temp_path / "codex"
        self.server.run_state = "RUN_STATE_IDLE"
        self.server.calls.clear()
        self.stub_fail.unlink(missing_ok=True)
        self.exec_log.unlink(missing_ok=True)
        self.cli_env = {**self.env, "AGY_DELEGATE_STATE_DIR": str(self.state), "AGY_DELEGATE_BRAIN_DIR": str(self.brain), "CODEX_HOME": str(self.codex)}
        self.transcript = self.brain / self.server.cid / ".system_generated/logs/transcript_full.jsonl"

    def run_cli(self, *parts, code=0, **env):
        run = subprocess.run([sys.executable, CLI, *parts, "--json-output"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, env={**self.cli_env, **env}, timeout=8)
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def start(self, *parts):
        return self.run_cli("start", "--cwd", str(self.work), "--prompt", "test initial", "--background", *parts)

    def last_initial(self):
        return [body for method, body in self.server.calls if method == "CreateConversation"][-1]

    def write_first_reply(self, text="initial reply"):
        body = self.last_initial()
        append(self.transcript, user(body["prompt"][0]["text"]), reply(text, model=body["model"]))

    def test_permissions_health_and_concurrent_connections(self):
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.sock.stat().st_mode), 0o600)
        with socket.socket(socket.AF_UNIX) as slow:
            slow.connect(str(self.sock))
            slow.sendall(b'{"action": ')
            health = self.run_cli("health")
        self.assertEqual(health["state"], "ready")
        self.assertEqual(health["pid"], self.proc.pid)
        self.assertEqual(health["agentapi_path"], str(self.agentapi))
        self.assertEqual(health["default_project_id"], "default-project")
        self.assertNotIn(self.server.csrf, json.dumps(health))
        self.sock.chmod(0o660)
        try:
            self.assertIn("0600", self.run_cli("health", code=1)["error"])
        finally:
            self.sock.chmod(0o600)
        with tempfile.TemporaryDirectory(dir=SOCKET_TMP, prefix="agy-") as wrong:
            Path(wrong).chmod(0o755)
            run = subprocess.run([sys.executable, SIDECAR], env={**self.env, "AGY_DELEGATE_SOCKET": wrong + "/socket"}, capture_output=True, text=True, timeout=4)
            self.assertEqual(run.returncode, 1)
            self.assertIn("0700", run.stderr)

    def test_invalid_frame_does_not_stop_server(self):
        with patch.dict(os.environ, self.cli_env):
            for value in (b"not json\n", b"[]\n", b"x" * (sidecar.MAX_FRAME + 1)):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(str(self.sock))
                    client.sendall(value)
                    with client.makefile("rb") as reader:
                        self.assertFalse(json.loads(reader.readline())["ok"])
        self.assertEqual(self.run_cli("health")["pid"], self.proc.pid)

    def test_new_wait_result_and_send_baseline(self):
        task = self.start("--model", "carbon", "--title", "initial title")
        self.assertEqual(task["conversation_id"], self.server.cid)
        self.assertEqual(task["project_id"], "inner")
        self.assertEqual(task["project_match"], "matched")
        self.assertEqual(task["backend"], "ConversationService/CreateConversation")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(self.last_initial()["model"], "gemini-4-carbon-high")
        self.assertEqual(self.last_initial()["title"], "initial title")
        self.server.run_state = "RUN_STATE_RUNNING"
        self.write_first_reply()
        self.assertEqual(self.run_cli("status", task["task_id"])["run_state"], "RUN_STATE_RUNNING")
        self.server.run_state = "RUN_STATE_IDLE"
        self.assertEqual(self.run_cli("wait", task["task_id"], "--timeout", "1")["phase"], "complete")
        turn = self.run_cli("followup", "--conversation-id", self.server.cid, "--title", "next title", "--prompt", "recite", "--background")
        self.assertEqual(turn["task_id"], task["task_id"])
        self.assertEqual(turn["turn"], 2)
        self.assertEqual(turn["model"], "gemini-4-carbon-high")
        project, command = json.loads(self.exec_log.read_text())
        self.assertEqual(project, "inner")
        self.assertEqual(command[:2], ["--title=next title", self.server.cid])
        self.assertIn("recite", command[2])
        append(self.transcript, user(command[2]), reply("second reply"), user("human"), reply("unrelated"))
        result = self.run_cli("result", task["task_id"])
        self.assertEqual(result["baseline_line"], 2)
        self.assertEqual(result["reply"], "second reply")
        self.assertTrue(result["warnings"])
        self.assertIn("diff_stat", result["git"])

    def test_model_passthrough_and_unavailable_is_never_sent(self):
        self.start("--model", "provider-custom-full-id")
        self.assertEqual(self.last_initial()["model"], "provider-custom-full-id")
        error = self.run_cli("start", "--prompt", "test", "--model", "not-in-desktop", "--background", code=1)
        self.assertIn("No other model was used", error["error"])
        self.assertEqual(sum(method == "CreateConversation" for method, _ in self.server.calls), 1)

    def test_project_fallback_never_uses_another_sidecar(self):
        unused = self.root / "other-workspace"
        unused.mkdir(exist_ok=True)
        result = self.run_cli("start", "--cwd", str(unused), "--prompt", "no matching project", "--background")
        self.assertEqual(result["project_match"], "unmatched_default")
        self.assertEqual(self.last_initial()["projectId"], "default-project")

    def test_review_and_doc_scopes(self):
        repo = self.root / "review-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", repo, *args], stderr=subprocess.DEVNULL, text=True).strip()
        git("init", "--quiet")
        target = repo / "plan.md"
        target.write_text("original\n")
        git("add", "plan.md")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "original")
        revision = git("rev-parse", "HEAD")
        target.write_text("modified\n")
        self.run_cli("review", "--cwd", str(repo), "--uncommitted", "--background")
        brief = self.last_initial()["prompt"][0]["text"]
        self.assertIn("Read-only review: do not modify any files", brief)
        self.assertIn("M plan.md", brief)
        self.run_cli("review", "--cwd", str(repo), "--commit", revision, "--background")
        self.assertIn("Review only commit " + revision, self.last_initial()["prompt"][0]["text"])
        self.run_cli("doc", "--cwd", str(repo), "--file", "plan.md", "--include-git", "--prompt", "Polish the draft", "--background")
        brief = self.last_initial()["prompt"][0]["text"]
        self.assertIn(str(target), brief)
        self.assertIn("Do not modify any other files", brief)
        self.assertIn("git diff --stat", brief)

    def test_followup_uncertain_does_not_advance_codex_offset(self):
        session = str(uuid.uuid4())
        rollout = self.codex / "sessions" / f"rollout-test-{session}.jsonl"
        append(rollout, {"type": "event_msg", "payload": {"type": "user_message", "message": "first context"}})
        task = self.start("--codex-session", session)
        self.write_first_reply()
        append(rollout, {"type": "event_msg", "payload": {"type": "agent_message", "message": "second context"}})
        self.stub_fail.touch()
        self.run_cli("followup", task["task_id"], "--prompt", "test", "--background", code=1)
        disk = json.loads((self.state / (task["task_id"] + ".json")).read_text())
        self.assertEqual(disk["turn"]["phase"], "dispatch_unknown")
        self.assertEqual(disk["codex_offset"]["line"], 1)

    def test_followup_delta_and_reset(self):
        session = str(uuid.uuid4())
        rollout = self.codex / "sessions" / f"rollout-test-{session}.jsonl"
        append(rollout, {"type": "event_msg", "payload": {"type": "user_message", "message": "original context"}})
        task = self.start("--codex-session", session)
        self.write_first_reply()
        append(rollout, {"type": "event_msg", "payload": {"type": "agent_message", "message": "only delta"}})
        self.run_cli("followup", task["task_id"], "--prompt", "next", "--background")
        _, command = json.loads(self.exec_log.read_text())
        self.assertIn("only delta", command[-1])
        self.assertNotIn("original context", command[-1])
        append(self.transcript, user(command[-1]), reply("done"))
        rollout.write_text(rollout.read_text().replace("original context", "rewritten context"))
        result = self.run_cli("followup", task["task_id"], "--prompt", "next", "--background", code=1)
        self.assertIn("--reset-codex-offset", result["error"])
        self.run_cli("followup", task["task_id"], "--reset-codex-offset", "--prompt", "next", "--background")
        self.assertIn("rewritten context", json.loads(self.exec_log.read_text().splitlines()[-1])[1][-1])

    def test_transcript_matches_agentapi_system_message_and_ignores_reminders(self):
        log = self.root / "sysmsg.jsonl"
        marker = "AGY-DELEGATE:deadbeef"
        append(log,
               user("turn 1"),
               reply("old reply"),
               {"type": "SYSTEM_MESSAGE", "status": "DONE", "content": f"<SYSTEM_MESSAGE>\n[Message] content=[{marker}]\nfollow up\n</SYSTEM_MESSAGE>"},
               {"type": "SYSTEM_MESSAGE", "status": "DONE", "content": "<SYSTEM_MESSAGE>\nReminder: work efficiently.\n</SYSTEM_MESSAGE>"},
               reply("followup answer"),
               user("human interjection"),
               reply("after human"))
        two = self.root / "two.jsonl"
        append(two, user("turn 1"), reply("old reply"))
        base = cli.snapshot(two)
        info = cli.transcript_turn(log, base, marker)
        self.assertEqual(info["reply"], "followup answer")
        self.assertEqual(info["new_user_messages"], 2)
        self.assertTrue(info["warnings"])

    def test_wait_timeout_and_missing_or_refused_socket(self):
        task = self.start()
        for state in ("RUN_STATE_RUNNING", "RUN_STATE_CANCELLING", "RUN_STATE_IDLE"):
            self.server.run_state = state
            self.assertIn("Wait timed out", self.run_cli("wait", task["task_id"], "--timeout", "0.12", code=124)["error"])
        self.assertEqual(self.run_cli("list")["tasks"][0]["phase"], "submitted")
        offline = self.root / "offline"
        try:
            for refused in (False, True):
                if refused:
                    with socket.socket(socket.AF_UNIX) as dead:
                        dead.bind(str(offline))
                        offline.chmod(0o600)
                response = self.run_cli("review", "--cwd", str(self.work), "--prompt", "plan", "--fallback-brief", "--background", code=1, AGY_DELEGATE_SOCKET=str(offline))
                self.assertIn("Make sure Antigravity Desktop is running", response["error"])
                self.assertIn("Read-only review", response["fallback_brief"])
        finally:
            offline.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
