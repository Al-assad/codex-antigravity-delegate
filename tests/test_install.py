import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'install.sh'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='installer-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home with spaces'
        self.skills = self.home / '.agents/skills'
        self.gemini = self.home / '.gemini/config'
        self.config = self.gemini / 'config.json'
        self.sidecar = self.gemini / 'sidecars/agy-delegate'
        self.env = dict(os.environ, HOME=str(self.home), PATH=os.path.dirname(sys.executable) + os.pathsep + os.defpath)
        self.env.pop('AGENTS_SKILLS_DIR', None)
        self.env.pop('GEMINI_CONFIG_DIR', None)

    def run_install(self, *args, status=0):
        result = subprocess.run([INSTALLER, *args], env=self.env, cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        return result

    def read_config(self):
        return json.loads(self.config.read_text(encoding='utf-8'))

    def write_config(self, config):
        self.gemini.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(config), encoding='utf-8')

    def assert_skill_files(self, name):
        target = self.skills / name
        for relative in ('SKILL.md', f'scripts/{name.replace("-", "_")}.py'):
            self.assertEqual((target / relative).read_bytes(), (ROOT / 'skills' / name / relative).read_bytes())
        self.assertTrue(os.access(target / 'scripts' / f'{name.replace("-", "_")}.py', os.X_OK))

    def test_copy_all_and_uninstall(self):
        result = self.run_install('--copy')
        self.assertIn('health', result.stdout)
        for skill in ('codex-delegate', 'agy-delegate'):
            self.assertFalse((self.skills / skill).is_symlink())
            self.assert_skill_files(skill)
        self.assertFalse(self.sidecar.is_symlink())
        for path, mode in ((self.sidecar, 0o700), (self.sidecar / 'sidecar.json', 0o600),
                           (self.sidecar / 'sidecar.py', 0o700), (self.config, 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        installed = json.loads((self.sidecar / 'sidecar.json').read_bytes())
        template = json.loads((ROOT / 'sidecar/agy-delegate/sidecar.json').read_bytes())
        self.assertEqual(template['command'], 'python3')
        self.assertEqual(installed, {**template, 'command': os.path.abspath(sys.executable)})
        self.assertEqual(self.read_config(), {'sidecars': {'agy-delegate': {'enabled': True}}})

        self.run_install('--uninstall')
        self.assertFalse(self.sidecar.exists())
        self.assertFalse((self.skills / 'codex-delegate').exists())
        self.assertFalse((self.skills / 'agy-delegate').exists())
        self.assertEqual(self.read_config(), {'sidecars': {}})
        unchanged = self.config.stat()
        self.run_install('--uninstall')
        self.assertEqual(self.config.stat().st_ino, unchanged.st_ino)

    def test_symlink_and_switch_modes(self):
        self.run_install('--symlink')
        for skill in ('codex-delegate', 'agy-delegate'):
            target = self.skills / skill
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ROOT / 'skills' / skill)
            self.assert_skill_files(skill)
        self.assertFalse(self.sidecar.is_symlink())

        self.run_install('--copy')
        self.assertFalse((self.skills / 'codex-delegate').is_symlink())
        self.run_install('--symlink', '--only', 'codex')
        self.assertTrue((self.skills / 'codex-delegate').is_symlink())
        self.assertFalse((self.skills / 'agy-delegate').is_symlink())
        self.run_install('--uninstall')
        self.assertFalse((self.skills / 'codex-delegate').is_symlink())
        self.assertTrue((ROOT / 'skills/codex-delegate/SKILL.md').is_file())

    def test_only_codex_uses_default_copy_and_does_not_touch_gemini(self):
        self.run_install('--only', 'codex')
        self.assert_skill_files('codex-delegate')
        self.assertFalse((self.skills / 'codex-delegate').is_symlink())
        self.assertFalse((self.skills / 'agy-delegate').exists())
        self.assertFalse(self.gemini.exists())

    def test_only_agy_with_overrides(self):
        self.skills, self.gemini = self.root / 'skills override', self.root / 'config override'
        self.env.update(AGENTS_SKILLS_DIR=str(self.skills), GEMINI_CONFIG_DIR=str(self.gemini))
        self.run_install('--only', 'agy')
        self.assert_skill_files('agy-delegate')
        self.assertFalse((self.skills / 'codex-delegate').exists())
        self.assertTrue((self.gemini / 'sidecars/agy-delegate/sidecar.json').is_file())
        self.assertFalse(self.home.exists())

    def test_idempotent_merge_preserves_settings_and_uninstall_disables_own_entry(self):
        self.write_config({'ui': {'language': 'en'}, 'sidecars': {
            'agy-delegate': {'enabled': False, 'projectId': 'test-project', 'custom': {'retry': 2}},
            'other': {'enabled': True, 'projectId': 'other-project'},
        }})
        expected = self.read_config()
        self.config.chmod(0o640)
        self.run_install()
        expected['sidecars']['agy-delegate']['enabled'] = True
        self.assertEqual(self.read_config(), expected)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        tracked = [self.config, self.sidecar, self.sidecar / 'sidecar.json', self.skills / 'agy-delegate']
        before = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in tracked]
        self.run_install()
        self.assertEqual([(path.stat().st_ino, path.stat().st_mtime_ns) for path in tracked], before)

        self.run_install('--uninstall', '--only', 'agy')
        expected['sidecars']['agy-delegate']['enabled'] = False
        self.assertEqual(self.read_config(), expected)
        self.assertFalse((self.skills / 'agy-delegate').exists())
        self.assertFalse(self.sidecar.exists())
        self.assert_skill_files('codex-delegate')

    def test_dry_run_does_not_write(self):
        result = self.run_install('--dry-run')
        self.assertIn('Would Copy', result.stdout)
        self.assertIn('Would Enable', result.stdout)
        self.assertFalse(self.home.exists())
        self.run_install()
        before = self.config.read_bytes()
        self.assertIn('Would Remove', self.run_install('--uninstall', '--dry-run').stdout)
        self.assertEqual(self.config.read_bytes(), before)
        self.assert_skill_files('agy-delegate')
        self.assertTrue(self.sidecar.exists())

    def test_refuses_to_replace_or_uninstall_unmanaged_data(self):
        foreign = self.skills / 'agy-delegate'
        foreign.mkdir(parents=True)
        owned_by_user = foreign / 'keep.txt'
        owned_by_user.write_text('keep this', encoding='utf-8')
        for args in (('--copy',), ('--symlink',), ('--uninstall',)):
            with self.subTest(args=args):
                self.assertIn('unmanaged', self.run_install(*args, status=1).stderr)
                self.assertEqual(owned_by_user.read_text(encoding='utf-8'), 'keep this')
                self.assertFalse((self.skills / 'codex-delegate').exists())
                self.assertFalse(self.config.exists())

    def test_uninstall_does_not_remove_locally_edited_copy(self):
        self.run_install('--only', 'agy')
        script = self.sidecar / 'sidecar.py'
        original = script.read_bytes()
        script.write_bytes(original + b'\n# local edit\n')
        self.assertIn('were edited', self.run_install('--uninstall', status=1).stderr)
        self.assertTrue(script.read_bytes().endswith(b'# local edit\n'))
        self.assertTrue(self.read_config()['sidecars']['agy-delegate']['enabled'])
        self.assert_skill_files('agy-delegate')

    def test_malformed_config_leaves_destinations_untouched(self):
        self.gemini.mkdir(parents=True)
        for invalid in (b'{oops', b'{"sidecars": []}', b'{"sidecars":{ "agy-delegate": false }}', b'{"ui": 1, "ui": 2}'):
            with self.subTest(invalid=invalid):
                self.config.write_bytes(invalid)
                self.run_install(status=1)
                self.assertEqual(self.config.read_bytes(), invalid)
                self.assertFalse(self.skills.exists())
                self.assertFalse(self.sidecar.exists())

    def test_uninstall_does_not_disable_an_unmanaged_config_entry(self):
        self.write_config({'sidecars': {'agy-delegate': {'enabled': True, 'projectId': 'test-project'}}})
        before = self.config.read_bytes()
        self.run_install('--uninstall')
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
