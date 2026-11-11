"""Run on the Docker host after installing the dedicated listener; cleans test resources."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def run(*args, check=True, capture=True):
    return subprocess.run(args, check=check, capture_output=capture, text=True)


def main():
    root = Path(__file__).resolve().parent
    env = dict(line.split('=', 1) for line in (root.parent / '.env').read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    for key in ('HOST_PROXY_USER', 'HOST_PROXY_PASS', 'HOST_PROXY_PORT'):
        os.environ[key] = env[key]
    prefix = 'test-egress-' + uuid.uuid4().hex[:8]
    image, internal, external = prefix + ':test', prefix + '-internal', prefix + '-external'
    proxy, normal, privileged = prefix + '-proxy', prefix + '-normal', prefix + '-privileged'
    # Test subnet reproduces the production lower/upper /25 split without overlapping it.
    subnet = '172.29.251'
    try:
        build = run('docker', 'build', '-t', image, str(root), check=False)
        if build.returncode:
            print(build.stdout, build.stderr)
            raise RuntimeError('Image build failed')
        run('docker', 'network', 'create', '--internal', '--subnet', subnet + '.0/24', internal)
        run('docker', 'network', 'create', external)
        run('docker', 'run', '-d', '--name', proxy, '--network', external,
            '--add-host', 'host.docker.internal:host-gateway',
            '-e', 'HOST_PROXY_USER', '-e', 'HOST_PROXY_PASS', '-e', 'HOST_PROXY_PORT',
            '-e', 'PRIVILEGED_CIDR=' + subnet + '.128/25', image)
        run('docker', 'network', 'connect', '--ip', subnet + '.2', internal, proxy)
        for client, ip in ((normal, '.10'), (privileged, '.130')):
            run('docker', 'run', '-d', '--name', client, '--network', internal, '--ip', subnet + ip,
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--entrypoint', 'python', image, '-c', 'import time; time.sleep(600)')
        request = '''import json,sys,urllib.request,urllib.error
url,proxy=sys.argv[1:]
opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
try:
 with opener.open(url,timeout=12) as r: print(json.dumps({'ok':True,'status':r.status}))
except Exception as e: print(json.dumps({'ok':False,'error':str(e)}))
'''
        def probe(client, url, use_proxy=True):
            result = run('docker', 'exec', client, 'python', '-c', request, url,
                         'http://' + subnet + '.2:7890' if use_proxy else '')
            return json.loads(result.stdout)
        for _ in range(20):
            result = probe(normal, 'https://www.baidu.com')
            if result['ok']:
                break
            time.sleep(1)
        assert result['ok'], ('mainland domain failed', result)
        print('PASS normal mainland https://www.baidu.com (DIRECT):', result['status'])
        result = probe(normal, 'https://example.com')
        assert result['ok'], result
        print('PASS normal non-mainland https://example.com (host proxy):', result['status'])
        result = probe(privileged, 'https://example.com')
        assert result['ok'], result
        print('PASS privileged https://example.com:', result['status'])
        for client in (normal, privileged):
            result = probe(client, 'http://1.1.1.1', False)
            assert not result['ok'], result
        print('PASS both internal-only clients: direct public-IP internet unavailable')
        for url in ('https://example.com:8443', 'http://' + subnet + '.130:80', 'https://10.0.0.1'):
            result = probe(normal, url)
            assert not result['ok'], result
            print('PASS normal rejected by policy:', url, result['error'])
    finally:
        for container in (normal, privileged, proxy):
            run('docker', 'rm', '-f', container, check=False)
        for network in (internal, external):
            run('docker', 'network', 'rm', network, check=False)
        run('docker', 'image', 'rm', image, check=False)


if __name__ == '__main__':
    main()
