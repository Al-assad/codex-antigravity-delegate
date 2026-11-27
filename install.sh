#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./install.sh [--copy | --symlink] [--only all|codex|agy] [--uninstall] [--dry-run]

  --copy       Copy skills (default; the checkout is not needed at runtime).
  --symlink    Link skills to this checkout. The Sidecar is always copied and
               its manifest uses the absolute path of the active Python 3.
  --only       Select all (default), codex, or agy (includes its Sidecar).
  --uninstall  Remove managed files. Preserve other Antigravity settings.
  --dry-run    Show planned changes without writing anything.

Overrides: AGENTS_SKILLS_DIR and GEMINI_CONFIG_DIR (defaults under HOME).
Requires Python 3.9 or newer. Existing unmanaged or locally edited installs
are not overwritten or removed. Back them up and move them aside first.
EOF
}

mode=copy
only=all
uninstall=no
dry_run=no
while [ "$#" -gt 0 ]; do
    case "$1" in
        --copy) mode=copy ;;
        --symlink) mode=symlink ;;
        --only)
            if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
            only=$2
            case "$only" in all|codex|agy) ;; *) usage >&2; exit 2 ;; esac
            shift ;;
        --uninstall) uninstall=yes ;;
        --dry-run) dry_run=yes ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

if ! python=$(command -v python3); then
    printf '%s\n' 'install.sh: Python 3.9 or newer is required.' >&2
    exit 1
fi
root=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd -P)

exec "$python" - "$root" "$mode" "$only" "$uninstall" "$dry_run" <<'PY'
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


MARKER = '.codex-antigravity-delegate-install.json'
SIDECAR = 'agy-delegate'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def unique_object(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), 'config.json contains duplicate keys; fix it before retrying')
    return result


def path_mode(path):
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Package:
    def __init__(self, source, target, paths, link=False, private=False):
        self.source, self.target, self.paths = source, target, paths
        self.link, self.private = link, private
        self.ident = f'{source.parent.name}/{source.name}'
        require(source != target, f'install target is the source directory: {target}')

    def inventory(self):
        mode = path_mode(self.target)
        if mode is None:
            return None
        if stat.S_ISLNK(mode):
            require(not self.private and self.target.resolve() == self.source,
                    f'will not replace or remove an unmanaged symlink: {self.target}')
            return ('symlink', os.readlink(self.target))
        require(stat.S_ISDIR(mode), f'will not replace or remove an unmanaged path: {self.target}')
        marker = self.target / MARKER
        require(stat.S_ISREG(path_mode(marker) or 0), f'will not touch an unmanaged directory: {self.target}')
        receipt = json.loads(marker.read_bytes())
        require(isinstance(receipt, dict) and receipt.get('package') == self.ident,
                f'invalid install receipt: {marker}')
        files = {self.target / relative: relative for relative in self.paths}
        parents = {path.parent for path in files} - {self.target}
        found = set(self.target.iterdir())
        for parent in parents:
            require(stat.S_ISDIR(path_mode(parent) or 0), f'installed directory was changed: {parent}')
            found.update(parent.iterdir())
        require(found == set(files) | parents | {marker}, f'installed directory contents changed: {self.target}')
        current = {}
        modes = {}
        for path, relative in files.items():
            file_mode = path_mode(path) or 0
            require(stat.S_ISREG(file_mode), f'installed file was changed: {path}')
            current[relative] = digest(path.read_bytes())
            modes[relative] = stat.S_IMODE(file_mode)
        require(current == receipt.get('files'), f'installed files were edited: {self.target}')
        return ('copy', current, modes, stat.S_IMODE(mode), stat.S_IMODE(marker.lstat().st_mode))

    def plan(self, uninstall):
        self.before = self.inventory()
        if uninstall:
            return 'Remove' if self.before else None
        if self.link:
            return None if self.before and self.before[0] == 'symlink' else 'Link'
        self.payload = {name: (self.source / name).read_bytes() for name in self.paths}
        if self.private:
            manifest = json.loads(self.payload['sidecar.json'])
            manifest['command'] = os.path.abspath(sys.executable)
            self.payload['sidecar.json'] = json_bytes(manifest)
        self.hashes = {name: digest(data) for name, data in self.payload.items()}
        same = ('copy', self.hashes, self.paths, 0o700 if self.private else 0o755, 0o600)
        return None if self.before == same else 'Copy'

    def stage(self, stack, action):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.delegate-', dir=self.target.parent)))
        new = stage / 'new'
        if action == 'Link':
            new.symlink_to(self.source, target_is_directory=True)
        elif action == 'Copy':
            new.mkdir()
            new.chmod(0o700 if self.private else 0o755)
            for name, data in self.payload.items():
                path = new / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(self.paths[name])
            marker = new / MARKER
            marker.write_bytes(json_bytes({'package': self.ident, 'files': self.hashes}))
            marker.chmod(0o600)
        return stage


def config_plan(path, uninstall):
    mode = path_mode(path)
    require(mode is None or stat.S_ISREG(mode), f'expected a regular config file: {path}')
    old = None if mode is None else path.read_bytes()
    config = {} if old is None else json.loads(old, object_pairs_hook=unique_object)
    require(isinstance(config, dict), 'config.json must contain a JSON object')
    sidecars = config.get('sidecars', {})
    require(isinstance(sidecars, dict), 'config.json: sidecars must be an object')
    current = sidecars.get(SIDECAR, {})
    require(isinstance(current, dict), f'config.json: sidecars.{SIDECAR} must be an object')
    if uninstall:
        if not any(key != 'enabled' for key in current):
            if SIDECAR not in sidecars:
                return None
            del sidecars[SIDECAR]
        elif current.get('enabled') is False:
            return None
        else:
            current['enabled'] = False
    else:
        if current.get('enabled') is True:
            return None
        sidecars[SIDECAR] = {**current, 'enabled': True}
        config['sidecars'] = sidecars
    return old, mode, json_bytes(config)


def write_config(path, plan):
    old, mode, data = plan
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.delegate-', dir=path.parent) as directory:
        staged = Path(directory) / 'config.json'
        with staged.open('wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), stat.S_IMODE(mode) if mode is not None else 0o600)
            stream.flush()
            os.fsync(stream.fileno())
        require(path_mode(path) == mode and (mode is None or path.read_bytes() == old),
                f'configuration changed while installing; retry: {path}')
        os.replace(staged, path)


def main():
    require(sys.version_info >= (3, 9), 'Python 3.9 or newer is required')
    root, mode, only, uninstall, dry_run = sys.argv[1:]
    root = Path(root)
    uninstall, dry_run = uninstall == 'yes', dry_run == 'yes'
    skills = Path(os.environ.get('AGENTS_SKILLS_DIR') or Path.home() / '.agents/skills').expanduser().resolve()
    gemini = Path(os.environ.get('GEMINI_CONFIG_DIR') or Path.home() / '.gemini/config').expanduser().resolve()
    packages = []
    for skill in ('codex', 'agy'):
        if only in ('all', skill):
            name = f'{skill}-delegate'
            script = f'scripts/{skill}_delegate.py'
            packages.append(Package(root / 'skills' / name, skills / name,
                                    {'SKILL.md': 0o644, script: 0o755}, link=mode == 'symlink'))
    sidecar = None
    if only in ('all', 'agy'):
        sidecar = Package(root / 'sidecar' / SIDECAR, (gemini / 'sidecars').resolve() / SIDECAR,
                          {'sidecar.json': 0o600, 'sidecar.py': 0o700}, private=True)
        packages.append(sidecar)
    targets = [package.target for package in packages]
    require(not any(left == right or left in right.parents for index, left in enumerate(targets)
                    for other, right in enumerate(targets) if index != other), 'install targets overlap')
    actions = [(package, package.plan(uninstall)) for package in packages]
    config_file = gemini / 'config.json'
    plan = config_plan(config_file, uninstall) if sidecar and (not uninstall or sidecar.before) else None
    prefix = 'Would ' if dry_run else ''
    for package, action in actions:
        print(f'{prefix}{action or "Keep"}: {package.target}')
    if plan is not None:
        print(f'{prefix}{"Disable" if uninstall else "Enable"} {SIDECAR} in {config_file}')
    if dry_run:
        return
    with ExitStack() as staging:
        staged = [(package, package.stage(staging, action)) for package, action in actions if action]
        applied = []
        try:
            for package, stage in staged:
                target = package.target
                require(package.inventory() == package.before, f'install target changed; retry: {target}')
                old, new = stage / 'old', stage / 'new'
                if package.before:
                    os.replace(target, old)
                applied.append((target, old, new))
                if path_mode(new) is not None:
                    os.replace(new, target)
            if plan is not None:
                write_config(config_file, plan)
        except Exception:
            for target, old, new in reversed(applied):
                if path_mode(target) is not None:
                    os.replace(target, new)
                if path_mode(old) is not None:
                    os.replace(old, target)
            raise
    if sidecar and not uninstall:
        cli = shlex.quote(str(skills / SIDECAR / 'scripts/agy_delegate.py'))
        print(f'Next: run {cli} health. If the Sidecar is offline, restart Antigravity Desktop and retry.')
    elif sidecar and sidecar.before:
        print('Restart Antigravity Desktop if the removed Sidecar is still running.')


try:
    main()
except (OSError, ValueError, RuntimeError) as exc:
    sys.exit(f'install.sh: {exc}')
PY
