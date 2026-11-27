import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/codex-delegate/scripts/codex_delegate.py'
SPEC = importlib.util.spec_from_file_location('codex_delegate', SCRIPT)
delegate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delegate)


class CliDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.desktop = self.root / 'desktop' / 'codex'
        self.on_path = self.root / 'bin' / 'codex'
        self.nvm = self.root / '.nvm/versions/node'
        self.use_patch(patch.dict(os.environ, {'HOME': str(self.root), 'PATH': str(self.on_path.parent)}, clear=True))
        self.use_patch(patch.object(delegate, 'DESKTOP_CODEX', self.desktop))
        self.use_patch(patch.object(sys, 'platform', 'darwin'))

    def use_patch(self, context):
        context.start()
        self.addCleanup(context.stop)

    def executable(self, path, mode=0o700):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        path.chmod(mode)
        return str(path)

    def test_explicit_override_then_desktop_then_path_then_nvm(self):
        nvm = self.executable(self.nvm / 'v24.1.0/bin/codex')
        on_path = self.executable(self.on_path)
        desktop = self.executable(self.desktop)
        override = self.executable(self.root / 'override')
        with patch.dict(os.environ, {'CODEX_DELEGATE_CODEX': '~/override'}):
            self.assertEqual(delegate.cli_path(), override)
        self.assertEqual(delegate.cli_path(), desktop)
        self.desktop.unlink()
        self.assertEqual(delegate.cli_path(), on_path)
        self.on_path.unlink()
        self.assertEqual(delegate.cli_path(), nvm)

    def test_latest_executable_nvm_version(self):
        for version in ('v9.20.0', 'v24.9.0', 'v24.12.0-rc.10', 'unknown'):
            self.executable(self.nvm / version / 'bin/codex')
        newest = self.executable(self.nvm / 'v24.12.0/bin/codex')
        self.executable(self.nvm / 'v25.0.0/bin/codex', mode=0o600)
        self.assertEqual(delegate.cli_path(), newest)

    def test_missing_override_does_not_silently_choose_another_cli(self):
        self.executable(self.on_path)
        with patch.dict(os.environ, {'CODEX_DELEGATE_CODEX': '/missing/codex'}):
            with self.assertRaises(delegate.DelegateError):
                delegate.cli_path()


STUB = '''
import json, os, pathlib, signal, subprocess, sys, time
from uuid import uuid4

args = sys.argv[1:]
prompt = sys.stdin.read()
session = args[-2] if 'resume' in args else str(uuid4())
home = pathlib.Path(os.environ['CODEX_HOME'])
rollouts = home / 'sessions'
rollout = next(rollouts.rglob(f'rollout-*-{session}.jsonl'),
               rollouts / f'rollout-stub-{session}.jsonl')
rollout.parent.mkdir(parents=True, exist_ok=True)
out = pathlib.Path(args[args.index('-o') + 1])
out.with_name('brief.seen').write_text(prompt, encoding='utf-8')
out.with_name('argv.seen').write_text(json.dumps(args), encoding='utf-8')
with rollout.open('a', encoding='utf-8') as stream:
    model = args[args.index('-m') + 1] if '-m' in args else 'stub-default'
    for payload in [
        {'type': 'turn_context', 'payload': {'cwd': os.getcwd(), 'model': model,
         'sandbox_policy': {'type': args[args.index('-s') + 1]}}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': prompt}]}},
    ]:
        stream.write(json.dumps(payload) + '\\n')

def event(kind, **data):
    print(json.dumps({'type': kind, **data}), flush=True)

event('thread.started', thread_id=session)
event('turn.started')
if 'RUN_LONG' in prompt:
    pathlib.Path('written-by-stub').write_text('not rolled back')
    child_code = 'import time; time.sleep(120)'
    if 'IGNORE_TERM' in prompt:
        child_code = 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); ' + child_code
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', child_code])
    out.with_name('child.pid').write_text(str(child.pid))
    time.sleep(120)
if 'FAIL_TURN' in prompt:
    event('error', message='stub error')
    event('turn.failed', error={'message': 'stub failure'})
    sys.exit(1)
event('item.completed', item={'id': 'item_1', 'type': 'agent_message', 'text': 'EVENT REPLY'})
if 'EVENT_ONLY' not in prompt:
    out.write_text('FILE REPLY', encoding='utf-8')
event('turn.completed', usage={'input_tokens': 1, 'output_tokens': 1})
'''


class ParsingTests(unittest.TestCase):
    def test_command_effort_model_and_resume_flag_positions(self):
        for effort in delegate.EFFORTS:
            command = delegate.build_command('/bin/codex', '/a path', 'workspace-write',
                                             'sol', effort, '/a reply')
            self.assertIn(f'model_reasoning_effort="{effort}"', command)
            self.assertIn('gpt-6.2-sol', command)
            self.assertEqual(command[-1], '-')
        session = str(uuid4())
        resume = delegate.build_command('/bin/codex', '/a path', 'read-only',
                                        'gpt-6.1-sol', None, '/a reply', resume=session, non_git=True)
        self.assertEqual(resume[:7], ['/bin/codex', 'exec', '-C', '/a path', '-s', 'read-only', 'resume'])
        self.assertIn('gpt-6.1-sol', resume)
        self.assertIn('--skip-git-repo-check', resume)
        self.assertEqual(resume[-2:], [session, '-'])
        self.assertIn('gpt-6.2-astra', delegate.build_command('/bin/codex', '/a', 'read-only',
                                                            'astra', None, '/reply'))

    def test_event_session_and_malformed_or_partial_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            path.write_text('not-a-json-line\n'
                            '{"type":"thread.started","thread_id":"thread-id"}\n'
                            '{"type":"item.completed","item":{"type":"agent_message","text":"Hi"}}\n'
                            '{"type":"turn.failed","error":{"message":"bad"}}\n'
                            '{"type":"turn.completed"}')
            result = delegate.event_summary(path)
            self.assertEqual(result['session_id'], 'thread-id')
            self.assertEqual(result['event_reply'], 'Hi')
            self.assertEqual(result['terminal_event'], 'turn.failed')
            self.assertEqual(result['event_error'], 'bad')
            self.assertEqual(result['event_count'], 3)

    def test_codex_transcript_hides_instructions_and_deduplicates(self):
        rows = [
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
             'content': [{'type': 'input_text', 'text': 'developer noise'}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': '<environment_context>internal'}]}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'hello'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'hello'}]}},
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'text': 'private'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'hi'}]}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rollout.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            messages, cursor = delegate.transcript(path, 'codex')
            self.assertEqual([(m['role'], m['text']) for m in messages],
                             [('user', 'hello'), ('assistant', 'hi')])
            self.assertEqual(cursor['line'], 6)

    def test_incremental_budget_does_not_skip_unsent_rows(self):
        rows = [{'type': 'USER_INPUT', 'content': char * 20} for char in 'ABC']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'transcript_full.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            first, cursor = delegate.transcript(path, 'agy', since=0, limit=25)
            second, next_cursor = delegate.transcript(path, 'agy', checkpoint=cursor, limit=25)
            self.assertEqual([row['text'] for row in first + second], ['A' * 20, 'B' * 20])
            self.assertEqual((cursor['line'], next_cursor['line']), (1, 2))


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='delegate-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'tracked.txt').write_text('baseline')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial')
        self.state = self.root / 'state'
        self.home = self.root / 'codexhome'
        self.brain = self.root / 'desktop' / 'brain'
        self.acp = self.root / 'providers' / 'antigravity'
        stub = self.root / 'fake-codex'
        stub.write_text(f'#!{sys.executable}\n' + STUB)
        stub.chmod(0o700)
        self.env = dict(os.environ, CODEX_DELEGATE_CODEX=str(stub), CODEX_DELEGATE_STATE_DIR=str(self.state),
                        CODEX_HOME=str(self.home), CODEX_DELEGATE_AGY_BRAIN=str(self.brain),
                        CODEX_DELEGATE_AGY_ACP_ROOT=str(self.acp))
        self.addCleanup(self.clean_running)

    def clean_running(self):
        for ledger in self.state.glob('*/task.json'):
            task = json.loads(ledger.read_text())
            if task['turns'][-1]['status'] == 'running':
                self.invoke('cancel', task['task_id'], '--grace', '0.2', status=(0, 1, 124))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def invoke(self, *args, status=(0,), stdin=''):
        process = subprocess.run([sys.executable, str(SCRIPT), *args, '--json'], cwd=self.root,
                                 env=self.env, input=stdin, capture_output=True, text=True, timeout=20)
        self.assertIn(process.returncode, status, (args[0], process.stdout, process.stderr))
        return json.loads(process.stdout)

    def open_ledger(self, task_id):
        return json.loads((self.state / task_id / 'task.json').read_text())

    def seen(self, outcome, name='brief.seen'):
        return (Path(outcome['logs']['events']).parent / name).read_text()

    def start(self, prompt, *args):
        return self.invoke('start', '--cwd', str(self.repo), '--prompt', prompt, *args)

    def create_agy(self, *, acp=False):
        session = str(uuid4())
        root = self.acp / 'hash' / 'antigravity-acp' / 'brain' if acp else self.brain
        path = root / session / '.system_generated/logs/transcript_full.jsonl'
        path.parent.mkdir(parents=True)
        path.touch()
        return session, path

    def append_agy(self, path, kind, text=None):
        with path.open('a', encoding='utf-8') as stream:
            row = {'type': kind, 'status': 'DONE', 'created_at': '2026-11-27T00:00:00Z'}
            if text is not None:
                row['content'] = text
            if kind == 'PLANNER_RESPONSE':
                row['thinking'] = 'not shared'
            stream.write(json.dumps(row) + '\n')

    def test_shared_foreground_stdin_and_event_fallback(self):
        view = self.invoke('start', '--cwd', str(self.repo), '--include-git', '--model', 'astra',
                           '--effort', 'max', stdin='EVENT_ONLY from stdin')
        self.assertEqual(view['status'], 'completed')
        self.assertEqual(view['reply'], 'EVENT REPLY')
        self.assertEqual(view['actual_model'], 'gpt-6.2-astra')
        self.assertEqual(view['sandbox'], 'workspace-write')
        self.assertEqual(view['cwd'], str(self.repo))
        self.assertIn('EVENT_ONLY from stdin', self.seen(view))
        self.assertIn(self.git('rev-parse', 'HEAD'), self.seen(view))
        self.assertFalse((Path(view['logs']['events']).parent / 'prompt.tmp').exists())
        self.assertEqual(self.invoke('list')[0]['task_id'], view['task_id'])

    def test_dirty_worktree_default_rejects_then_isolates_from_source(self):
        (self.repo / 'tracked.txt').write_text('dirty')
        (self.repo / 'untracked').write_text('not in HEAD')
        denied = self.invoke('start', '--cwd', str(self.repo), '--worktree', '--prompt', 'task', status=(2,))
        self.assertIn('未提交改动', denied['error'])
        result = self.start('task', '--worktree', '--allow-dirty-worktree')
        isolated = Path(result['worktree'])
        self.assertEqual((isolated / 'tracked.txt').read_text(), 'baseline')
        self.assertFalse((isolated / 'untracked').exists())
        self.assertFalse(result['git']['dirty'])
        self.assertEqual((self.repo / 'tracked.txt').read_text(), 'dirty')
        follow = self.invoke('followup', result['task_id'], '--prompt', 'again')
        self.assertEqual(follow['cwd'], str(isolated))
        self.assertEqual(follow['session_id'], result['session_id'])

    def test_review_scope_custom_prompt_and_sandbox_adapter(self):
        result = self.invoke('review', '--cwd', str(self.repo), '--base', 'HEAD', '--model', 'gpt-6-sol',
                             '--prompt', '重点检查边界条件')
        args = json.loads(self.seen(result, 'argv.seen'))
        self.assertNotIn('review', args)
        self.assertEqual(args[args.index('-s') + 1], 'read-only')
        self.assertIn('重点检查边界条件', self.seen(result))
        self.assertIn(self.git('rev-parse', 'HEAD'), self.seen(result))
        commit = self.invoke('review', '--cwd', str(self.repo), '--commit', 'HEAD')
        self.assertIn('只审查提交', self.seen(commit))
        rejected = self.invoke('review', '--cwd', str(self.repo), '--uncommitted', '--worktree', status=(2,))
        self.assertIn('共享工作区', rejected['error'])

    def test_followup_advances_agy_only_when_successful(self):
        session, path = self.create_agy()
        self.append_agy(path, 'USER_INPUT', 'old user record')
        self.append_agy(path, 'SYSTEM_MESSAGE', 'noise')
        self.append_agy(path, 'PLANNER_RESPONSE', 'old planner record')
        with path.open('a') as stream:
            stream.write('{"type":"USER_INPUT","content":"partial')
        start = self.start('start task', '--agy-session', session)
        self.assertIn('old planner record', self.seen(start))
        self.assertNotIn('not shared', self.seen(start))
        self.assertNotIn('noise', self.seen(start))
        self.assertEqual(start['agy_synced_through_line'], 3)
        with path.open('a') as stream:
            stream.write(' complete"}\n')
        self.append_agy(path, 'PLANNER_RESPONSE', 'second planner record')
        task_id = start['task_id']
        failed = self.invoke('followup', task_id, '--prompt', 'FAIL_TURN', status=(1,))
        self.assertEqual(failed['agy_synced_through_line'], 3)
        self.assertEqual(failed['agy_pending_through_line'], 5)
        self.assertEqual(failed['error'], 'stub failure')
        follow = self.invoke('followup', '--session-id', f"codex://threads/{start['session_id']}",
                             '--prompt', 'retry', '--model', 'gpt-6-sol', '--effort', 'ultra')
        self.assertEqual((follow['task_id'], follow['number']), (task_id, 3))
        self.assertEqual(follow['session_id'], start['session_id'])
        self.assertIn('partial complete', self.seen(follow))
        self.assertIn('second planner record', self.seen(follow))
        self.assertNotIn('old planner record', self.seen(follow))
        self.assertEqual(self.open_ledger(task_id)['agy']['cursor']['line'], 5)
        again = self.invoke('followup', task_id, '--prompt', 'last')
        self.assertNotIn('second planner record', self.seen(again))
        self.assertEqual(self.invoke('result', task_id, '--turn', '1')['reply'], 'FILE REPLY')

    def test_rewritten_agy_log_requires_explicit_reset(self):
        session, path = self.create_agy()
        self.append_agy(path, 'USER_INPUT', 'first')
        start = self.start('task', '--agy-session', session)
        path.write_text('')
        self.append_agy(path, 'USER_INPUT', 'replacement')
        task_id = start['task_id']
        denied = self.invoke('followup', task_id, '--prompt', 'next', status=(2,))
        self.assertIn('--reset-agy-offset', denied['error'])
        reset = self.invoke('followup', task_id, '--prompt', 'next', '--reset-agy-offset')
        self.assertIn('replacement', self.seen(reset))

    def test_read_agy_acp_and_adopt_existing_codex_session(self):
        session, path = self.create_agy(acp=True)
        self.append_agy(path, 'USER_INPUT', 'first')
        self.append_agy(path, 'GENERIC', 'command output')
        self.append_agy(path, 'PLANNER_RESPONSE', 'answer')
        result = self.invoke('read-session', session, '--since-line', '1')
        self.assertEqual(result['source'], 'agy')
        self.assertEqual([row['text'] for row in result['messages']], ['answer'])
        self.assertEqual(result['next_since_line'], 3)
        session = str(uuid4())
        rollout = self.home / 'sessions' / f'rollout-example-{session}.jsonl'
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.repo)}}) + '\n')
        adopted = self.invoke('followup', '--session-id', session, '--prompt', 'new task existing session')
        self.assertEqual(adopted['session_id'], session)
        self.assertEqual(adopted['sandbox'], 'read-only')
        read = self.invoke('read-session', f'codex://threads/{session}', '--tail', '1')
        self.assertIn('new task existing session', read['messages'][0]['text'])

    def test_background_status_timeout_and_cancel_kills_process_group(self):
        start = self.start('RUN_LONG IGNORE_TERM', '--background')
        task = start['task_id']
        child_file = Path(start['logs']['events']).parent / 'child.pid'
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(child_file.read_text())
        status = self.invoke('status', task)
        self.assertEqual(status['status'], 'running')
        self.assertTrue(status['pid_alive'])
        self.assertIsNotNone(status['session_id'])
        limited = self.invoke('wait', task, '--timeout', '0.01', status=(124,))
        self.assertTrue(limited['timed_out'])
        cancelled = self.invoke('cancel', task, '--grace', '0.4')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(cancelled['sent_sigkill'])
        self.assertFalse(cancelled['pid_alive'])
        self.assertIn('written-by-stub', cancelled['git']['status_short'])
        self.assertIn('不会自动回滚', cancelled['notice'])
        self.assertEqual(self.invoke('wait', task, status=(130,))['status'], 'cancelled')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True,
                                   text=True).stdout.strip()
            if not state or state.startswith('Z'):
                break
            time.sleep(0.05)
        self.assertTrue(not state or state.startswith('Z'), f'子进程仍在运行：{state}')

    def test_background_wait_success_and_persistent_result(self):
        start = self.start('task', '--background')
        task_id = start['task_id']
        done = self.invoke('wait', task_id, '--timeout', '5')
        result = self.invoke('result', task_id)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(result['reply'], 'FILE REPLY')
        self.assertEqual(result['session_id'], done['session_id'])
        self.assertFalse(result['pid_alive'])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
