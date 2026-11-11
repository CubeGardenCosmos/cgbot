"""Run on the host with sudo; only adds this project's listener and source rule.

Requires host PyYAML and a running mihomo HTTP controller. Does not restart services.
Credentials are read from ../.env, never printed. Validation precedes replacement.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import yaml


def main():
    env_path = Path(__file__).resolve().parent.parent / '.env'
    env = dict(line.split('=', 1) for line in env_path.read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    port = int(env.get('HOST_PROXY_PORT', '7895'))
    subnet = env.get('EGRESS_SUBNET', '172.30.2.0/24')
    user, password = env['HOST_PROXY_USER'], env['HOST_PROXY_PASS']
    target = env.get('HOST_PROXY_GROUP')
    if not target:
        sys.exit('HOST_PROXY_GROUP is required in .env')
    
    path = Path('/etc/mihomo/config.yaml')
    original = path.read_bytes()
    config = yaml.safe_load(original)
    controller = config.get('external-controller', '')
    if not controller.startswith(('127.0.0.1:', 'localhost:')):
        raise SystemExit('A loopback HTTP controller is required; no service restart attempted')
    
    if target not in {group['name'] for group in config.get('proxy-groups', [])}:
        raise SystemExit(f'Expected host routing group "{target}" missing; no changes applied')
    
    listener = {'name': 'caged', 'type': 'mixed', 'port': port,
                'listen': '0.0.0.0', 'users': [{'username': user, 'password': password}],
                'proxy': target}
    listeners = config.setdefault('listeners', [])
    existing = [item for item in listeners if item.get('name') == 'caged']
    if existing and existing != [listener]:
        raise SystemExit('Existing project listener differs; refusing to overwrite')
    if any(item.get('port') == port and item.get('name') != 'caged' for item in listeners):
        raise SystemExit('Port occupied by another configured listener')
    if not existing:
        listeners.append(listener)
    rule = f'SRC-IP-CIDR,{subnet},{target},no-resolve'
    if rule not in config['rules']:
        config['rules'].insert(0, rule)
    updated = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode()
    if yaml.safe_load(original) == config:
        print('Project listener already configured; unchanged')
        return
    backup = path.with_name('config.yaml.caged-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    fd, temp = tempfile.mkstemp(prefix='caged-listener-', suffix='.yaml', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated)
        check = subprocess.run(['/usr/local/bin/mihomo', '-t', '-d', str(path.parent), '-f', temp],
                               capture_output=True)
        if check.returncode:
            raise SystemExit('mihomo validation failed; original untouched (credentials omitted)')
        headers = {'Content-Type': 'application/json'}
        if config.get('secret'):
            headers['Authorization'] = 'Bearer ' + config['secret']
        url = f'http://{controller}/configs?force=true'
        def reload_config():
            request = urllib.request.Request(url, data=json.dumps({'path': str(path)}).encode(),
                                             headers=headers, method='PUT')
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 204:
                    raise RuntimeError('Unexpected reload response')
        os.replace(temp, path)
        try:
            reload_config()
        except Exception:
            shutil.copy2(backup, path)
            reload_config()
            raise SystemExit('Reload failed; original restored, no restart attempted')
        print('Validated and hot-reloaded dedicated listener; timestamped backup retained')
    finally:
        Path(temp).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
