"""Stage E: verify a selected repository baseline at its pinned commit."""
# Reconstructed approximately from the preserved Python 3.14 bytecode.

from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class BaselinePreflightError(RuntimeError):
    pass

def _run(command, *, timeout, cwd=None):
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False, env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tempfile.gettempdir(), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'})

def _tail(text, limit=20000):
    return text[(-limit):]

def _repo_identity(idea):
    selection = idea.get('repository_selection')
    if selection is None:
        return None
    if not isinstance(selection, dict):
        raise ValueError('repository_selection must be null or an object')
    owner_and_name = selection.get('owner_and_name')
    sha = selection.get('pinned_commit_sha')
    url = selection.get('url')
    if (not isinstance(owner_and_name, str) or not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', owner_and_name)):
        raise ValueError('repository_selection.owner_and_name must be owner/repo')
    if (not isinstance(sha, str) or not re.fullmatch('[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', sha)):
        raise ValueError('repository_selection.pinned_commit_sha must be a full commit SHA')
    expected_url = f"https://github.com/{owner_and_name}"
    if url != expected_url:
        raise ValueError('repository_selection.url must match the canonical public GitHub URL')
    return (expected_url, owner_and_name, sha.lower())

def _load_baseline_config(path):
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('baseline config must be a JSON object')
    image = config.get('docker_image')
    commands = config.get('commands')
    if (not isinstance(image, str) or not image.strip()):
        raise ValueError('baseline config needs a preinstalled docker_image')
    if (not isinstance(commands, list) or not commands):
        raise ValueError('baseline config needs a non-empty commands array')
    for index, item in enumerate(commands):
        if (not isinstance(item, dict) or not isinstance(item.get('label'), str) or not item['label'].strip()):
            raise ValueError(f"baseline command {index} needs a non-empty label")
        if isinstance(item.get('command'), str) and item['command'].strip():
            continue
        raise ValueError(f"baseline command {index} needs a non-empty command string")
    timeout = config.get('timeout_seconds', 1800)
    memory_mb = config.get('memory_mb', 4096)
    cpus = config.get('cpus', 2)
    pids_limit = config.get('pids_limit', 256)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 7200:
        raise ValueError('timeout_seconds must be between 1 and 7200')
    if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or not 512 <= memory_mb <= 32768:
        raise ValueError('memory_mb must be between 512 and 32768')
    if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or not 0.5 <= cpus <= 8:
        raise ValueError('cpus must be between 0.5 and 8')
    if not isinstance(pids_limit, int) or isinstance(pids_limit, bool) or not 32 <= pids_limit <= 2048:
        raise ValueError('pids_limit must be between 32 and 2048')
    return {'docker_image': image, 'commands': commands, 'timeout_seconds': timeout, 'memory_mb': memory_mb, 'cpus': cpus, 'pids_limit': pids_limit}

def _run_in_container(repo_dir, config):
    if shutil.which('docker') is None:
        raise BaselinePreflightError('Docker is required for repository baseline commands')
    image = config['docker_image']
    inspected = _run(['docker', 'image', 'inspect', image], timeout=30)
    if inspected.returncode:
        raise BaselinePreflightError(f"Docker image {image!r} is not present locally; Stage E does not pull images or use networked builds")
    with tempfile.NamedTemporaryFile(prefix='taskforge-baseline-', suffix='.tar') as archive:
        archived = _run(['git', '-C', str(repo_dir), 'archive', '--format=tar', f"--output={archive.name}", 'HEAD'], timeout=60)
        if archived.returncode:
            raise BaselinePreflightError(f"could not archive pinned repository: {_tail(archived.stderr)}")
        container_name = f"taskforge-baseline-{os.getpid()}-{os.urandom(4).hex()}"
        create = ['docker', 'create', '--name', container_name, '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', f"--pids-limit={config['pids_limit']}", f"--memory={config['memory_mb']}m", f"--cpus={config['cpus']}", '--tmpfs', f"/tmp:rw,exec,nosuid,size={config['memory_mb']}m", '--tmpfs', f"/workspace:rw,exec,nosuid,size={config['memory_mb']}m", '--workdir=/workspace', '--entrypoint', 'sh', image, '-c', 'while :; do sleep 3600; done']
        created = _run(create, timeout=60)
        if created.returncode:
            raise BaselinePreflightError(f"could not create isolated baseline container: {_tail(created.stderr)}")
        results = []
        try:
            started = _run(['docker', 'start', container_name], timeout=30)
            if started.returncode:
                raise BaselinePreflightError(f"could not start baseline container: {_tail(started.stderr)}")
            copied = _run(['docker', 'cp', archive.name, f"{container_name}:/tmp/repository.tar"], timeout=120)
            if copied.returncode:
                raise BaselinePreflightError(f"could not copy pinned source into baseline container: {_tail(copied.stderr)}")
            extracted = _run(['docker', 'exec', container_name, 'sh', '-lc', 'tar -xf /tmp/repository.tar -C /workspace'], timeout=120)
            if extracted.returncode:
                raise BaselinePreflightError(f"could not unpack pinned source in baseline container: {_tail(extracted.stderr)}")
            for item in config['commands']:
                result = {'label': item['label'], 'command': item['command']}
                try:
                    completed = _run(['docker', 'exec', '--workdir=/workspace', container_name, 'sh', '-lc', item['command']], timeout=config['timeout_seconds'])
                    result.update({'returncode': completed.returncode, 'stdout_tail': _tail(completed.stdout), 'stderr_tail': _tail(completed.stderr), 'passed': completed.returncode == 0})
                except subprocess.TimeoutExpired as exc:
                    if not exc.stdout:
                        exc.stdout
                    if not exc.stderr:
                        exc.stderr
                    result.update({'passed': False, 'timed_out': True, 'timeout_seconds': config['timeout_seconds'], 'stdout_tail': _tail(str('')), 'stderr_tail': _tail(str(''))})
                results.append(result)
                if result['passed']:
                    continue
                break
            return results
        finally:
            _run(['docker', 'rm', '-f', container_name], timeout=30)

def run_stage_e(idea, output_dir, baseline_config_path=None):
    """Pin and verify an untouched repository, or record that no repo is needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = _repo_identity(idea)
    report = {'stage': 'D', 'created_at_utc': datetime.now(timezone.utc).isoformat(), 'repository_selection': idea.get('repository_selection')}
    if identity is None:
        report.update({'status': 'not_applicable', 'passed': True, 'summary': 'The idea is self-contained and does not select a repository.'})
    else:
        url, owner_and_name, sha = identity
        report.update({'repository_url': url, 'owner_and_name': owner_and_name, 'pinned_commit_sha': sha})
        if baseline_config_path is None:
            report.update({'status': 'needs_baseline_config', 'passed': False, 'summary': 'Provide documented build/test commands and a preinstalled offline Docker image.'})
        else:
            try:
                config = _load_baseline_config(baseline_config_path)
                if shutil.which('git') is None:
                    raise BaselinePreflightError('git is required for repository baseline preflight')
                with tempfile.TemporaryDirectory(prefix='taskforge-stage-e-') as temp_dir:
                    checkout = (Path(temp_dir) / 'repository')
                    clone = _run(['git', 'clone', '--no-checkout', '--filter=blob:none', url, str(checkout)], timeout=300)
                    if clone.returncode:
                        raise BaselinePreflightError(f"could not clone {url}: {_tail(clone.stderr)}")
                    fetched = _run(['git', '-C', str(checkout), 'fetch', '--depth=1', 'origin', sha], timeout=300)
                    if fetched.returncode:
                        raise BaselinePreflightError(f"could not fetch pinned commit {sha}: {_tail(fetched.stderr)}")
                    checked = _run(['git', '-C', str(checkout), 'checkout', '--detach', sha], timeout=120)
                    if checked.returncode:
                        raise BaselinePreflightError(f"could not check out pinned commit {sha}: {_tail(checked.stderr)}")
                    head = _run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], timeout=30)
                    actual_sha = head.stdout.strip().lower()
                    if head.returncode or actual_sha != sha:
                        raise BaselinePreflightError(f"checked out commit {actual_sha!r}, expected {sha!r}")
                    results = _run_in_container(checkout, config)
                passed = (all((item.get('passed') for item in results)) and len(results) == len(config['commands']))
                report.update({
                    'status': 'passed' if passed else 'failed',
                    'passed': passed,
                    'summary': 'All configured baseline commands passed.' if passed else 'A configured baseline command failed.',
                    'baseline_commands': results,
                    'sandbox': {'network': 'disabled', 'docker_image': config['docker_image'], 'memory_mb': config['memory_mb'], 'cpus': config['cpus'], 'pids_limit': config['pids_limit']},
                })
            except (ValueError, BaselinePreflightError, subprocess.TimeoutExpired) as exc:
                report.update({'status': 'failed', 'passed': False, 'summary': str(exc)})
    (output_dir / 'preflight.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return report
