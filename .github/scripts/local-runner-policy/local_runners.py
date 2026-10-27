#!/usr/bin/env python3
"""Reject workflow trees that can select anything except Workman local runners."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

VERSION = '1.0.0'
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
# Keep the verifier independent of whatever a target repository installs.
VENDORED_YAML = Path(__file__).resolve().with_name('pyyaml.zip')
if VENDORED_YAML.is_file():
    sys.path.insert(0, str(VENDORED_YAML))

def legacy_local_matrix_selector(field='matrix.runner'):
    """The earlier migration's exact selector has six static local-only outcomes.

    No general expression evaluation is performed. A changed expression must use
    explicit local labels instead of being accepted on a substring match.
    """
    value='('+field+')'
    choices=[(f"contains({value}, 'windows') && contains({value}, 'arm')",['self-hosted','Windows','ARM64','workman-ci']),
             (f"contains({value}, 'windows')",['self-hosted','Windows','X64','workman-ci']),
             (f"contains({value}, 'macos') && contains({value}, 'intel')",['self-hosted','macOS','X64','workman-ci']),
             (f"contains({value}, 'macos')",['self-hosted','macOS','ARM64','workman-ci']),
             (f"contains({value}, '-arm')",['self-hosted','Linux','ARM64','workman-ci'])]
    encoded=lambda labels:json.dumps(labels,separators=(',',':'))
    return "${{ fromJSON("+' || '.join(f"{condition} && '{encoded(labels)}'" for condition,labels in choices)+" || '"+encoded(['self-hosted','Linux','X64','workman-ci'])+"') }}"

def verified_legacy_selectors():
    fields=['matrix.runner','matrix.target.runner','inputs.runner',
            "inputs.arch == 'x64' && 'macos-15-intel' || 'macos-15'",
            "inputs.arch == 'arm64' && 'windows-latest-32-arm-core' || 'windows-latest-32-core'",
            "inputs.bundle-arch == 'x64' && 'macos-15-intel' || 'macos-15'",
            "inputs.bundle-arch == 'arm64' && 'windows-latest-32-arm-core' || 'windows-latest-32-core'"]
    result={legacy_local_matrix_selector(field) for field in fields}
    options=[('linux-arm64',['self-hosted','Linux','ARM64','workman-ci']),
             ('macos-arm64',['self-hosted','macOS','ARM64','workman-ci']),
             ('macos-x64',['self-hosted','macOS','X64','workman-ci'])]
    encoded=lambda labels:json.dumps(labels,separators=(',',':'))
    result.add("${{ fromJSON("+' || '.join(f"matrix.kind == '{kind}' && '{encoded(labels)}'" for kind,labels in options)+" || '"+encoded(['self-hosted','Linux','X64','workman-ci'])+"') }}")
    return frozenset(result)

VERIFIED_LEGACY_SELECTORS=verified_legacy_selectors()

class PolicyError(Exception):
    pass

def git(repo, *args):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if p.returncode:
        raise PolicyError('Git could not read the proposed commit')
    return p.stdout

def load_workflow(raw, path):
    try:
        import yaml
    except ImportError as error:
        raise PolicyError('PyYAML is unavailable; refusing an unverified push') from error
    class StrictLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                if key in result:
                    raise PolicyError(path + ': duplicate YAML mapping key')
                result[key] = loader.construct_object(value_node, deep=deep)
            except TypeError as error:
                raise PolicyError(path + ': unsupported YAML mapping key') from error
        return result
    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise PolicyError(path + ': workflow is too large to verify')
    try:
        value = yaml.load(raw.decode('utf-8'), Loader=StrictLoader)
    except PolicyError:
        raise
    except (UnicodeError, yaml.YAMLError, RecursionError) as error:
        raise PolicyError(path + ': YAML could not be verified') from error
    if not isinstance(value, dict):
        raise PolicyError(path + ': workflow must be a mapping')
    return value

def check_workflows(files):
    """files maps every workflow path in the proposed commit to raw bytes."""
    parsed = {path: load_workflow(raw, path) for path, raw in files.items()}
    visiting, done = set(), set()
    job_count = 0
    def inspect(path):
        nonlocal job_count
        if path in visiting:
            raise PolicyError(path + ': reusable workflow cycle')
        if path in done:
            return
        visiting.add(path)
        jobs = parsed[path].get('jobs')
        if not isinstance(jobs, dict) or not jobs:
            raise PolicyError(path + ': workflow has no verifiable jobs')
        for name, job in jobs.items():
            if not isinstance(name, str) or not isinstance(job, dict):
                raise PolicyError(path + ': unsupported job definition')
            where = path + ': job ' + name[:100]
            job_count += 1
            if 'uses' in job:
                target = job['uses']
                if 'runs-on' in job or not isinstance(target, str):
                    raise PolicyError(where + ': invalid reusable workflow job')
                if not target.startswith('./.github/workflows/'):
                    raise PolicyError(where + ': external reusable workflows cannot prove local runners; vendor the workflow locally')
                parts = PurePosixPath(target).parts
                target_path = '/'.join(parts)
                if '..' in parts or target_path not in parsed:
                    raise PolicyError(where + ': local reusable workflow is missing or invalid')
                inspect(target_path)
                continue
            selector = job.get('runs-on')
            if isinstance(selector,str) and selector in VERIFIED_LEGACY_SELECTORS:
                continue
            if isinstance(selector, dict):
                if set(selector) - {'group', 'labels'}:
                    raise PolicyError(where + ': unsupported runner selector')
                group = selector.get('group')
                if group is not None and (not isinstance(group, str) or '${{' in group):
                    raise PolicyError(where + ': dynamic runner groups are not allowed')
                selector = selector.get('labels')
            labels = [selector] if isinstance(selector, str) else selector
            if not isinstance(labels, list) or not labels or any(not isinstance(v, str) or '${{' in v for v in labels):
                raise PolicyError(where + ': runner labels must be explicit static values')
            if 'self-hosted' not in labels or 'workman-ci' not in labels:
                raise PolicyError(where + ': runs-on must require both self-hosted and workman-ci; hosted runners and fallback expressions are blocked')
        visiting.remove(path)
        done.add(path)
    for path in parsed:
        inspect(path)
    return {'workflows': len(files), 'jobs': job_count, 'policy': VERSION}

def check_tree(repo, revision):
    if not revision or revision.startswith('-'):
        raise PolicyError('Invalid proposed revision')
    commit = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise PolicyError('Proposed commit identity is unverified')
    entries = git(repo, 'ls-tree', '-r', '-z', commit, '--', '.github/workflows').split(b'\0')
    files = {}
    for entry in entries:
        if not entry:
            continue
        meta, rawpath = entry.split(b'\t', 1)
        path = rawpath.decode('utf-8')
        if not path.endswith(('.yml', '.yaml')):
            continue
        mode, kind, oid = meta.decode().split()
        if mode not in ('100644', '100755') or kind != 'blob':
            raise PolicyError(path + ': workflow must be a regular tracked file')
        files[path] = git(repo, 'cat-file', 'blob', oid)
    return {'commit': commit, **check_workflows(files)}

def owned_remote(value, owner='sgtworkman'):
    if '://' in value:
        path = urlsplit(value).path
    else:
        match = re.match(r'(?:[^/@:]+@)?[^/:]+:(.+)', value)
        if not match:
            return False
        path = match.group(1)
    parts = path.strip('/').split('/')
    return len(parts) == 2 and parts[0].lower() == owner.lower() and bool(parts[1])

def pre_push(repo, remote_url, payload):
    explicit = subprocess.run(['git','-C',str(repo),'config','--bool','--get','workman.localRunnerPolicy'],capture_output=True,text=True)
    if not owned_remote(remote_url) and explicit.stdout.strip() != 'true':
        return []
    proofs, seen = [], set()
    for line in payload.splitlines():
        columns = line.split()
        if len(columns) != 4:
            raise PolicyError('Push update list could not be verified')
        _local_ref, local_sha, remote_ref, remote_sha = columns
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', local_sha) or not re.fullmatch(r'[0-9a-fA-F]{40,64}', remote_sha):
            raise PolicyError('Push object identity could not be verified')
        if set(local_sha) == {'0'}:  # Deleting a ref executes no workflow.
            continue
        if local_sha in seen:
            continue
        seen.add(local_sha)
        proof = check_tree(repo, local_sha)
        proofs.append({'ref': remote_ref, **proof})
    return proofs

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-tree', metavar='REVISION')
    mode.add_argument('--pre-push', nargs=2, metavar=('REMOTE_NAME','REMOTE_URL'))
    args = parser.parse_args(argv)
    try:
        if args.pre_push:
            result = pre_push(Path(args.repo), args.pre_push[1], sys.stdin.read())
        else:
            result = check_tree(Path(args.repo), args.check_tree)
        print(json.dumps({'local_runner_policy': 'PASS', 'verified': result}, separators=(',',':')))
        return 0
    except (PolicyError, OSError, ValueError, RecursionError) as error:
        print('LOCAL RUNNER POLICY BLOCKED: ' + str(error))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
