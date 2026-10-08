"""Makefile orchestration for the existing five-node Lima benchmark cluster.

Builds/imports an OCI package containing QEMU and Mini OS, not a guest image
replacement. No Docker/Podman daemon or host kubectl is required. Kubernetes
keeps the warm workers; background processes here are only the port-forward
and benchmark ingress. Run ``make help`` for commands and configuration.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(roots):
    """Hash names and bytes, including additions/deletions, not just mtimes."""
    digest = hashlib.sha256()
    ignored = {'.git', '__pycache__', 'out', '.pytest_cache'}
    for label, root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob('*'))
        for path in paths:
            relative = Path(path.name) if root.is_file() else path.relative_to(root)
            if not path.is_file() or ignored.intersection(relative.parts):
                continue
            if path.suffix in {'.pyc', '.img'} or path.name in {'kernel.elf', 'kernel.bin'}:
                continue
            digest.update(f'{label}/{relative}\0'.encode())
            digest.update(path.read_bytes())
            digest.update(b'\0')
    return digest.hexdigest()


class Setup:
    def __init__(self):
        self.state = Path(os.environ.get('STATE_DIR', '.load-test')).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.control = os.environ.get('CONTROL', 'mini-control')
        self.servers = int(os.environ.get('SERVERS', '5'))
        self.manifest = os.environ.get('KUBE_MANIFEST', 'deploy/kubernetes.yaml')
        self.nodes = os.environ.get('NODES', 'mini-control mini-worker-1 mini-worker-2 mini-worker-3 mini-worker-4').split()
        self.python = os.environ.get('PYTHON', sys.executable)
        self.router_port = int(os.environ.get('ROUTER_PORT', '8080'))
        self.server_port = int(os.environ.get('SERVER_PORT', '8090'))
        self.router_url = f'http://127.0.0.1:{self.router_port}'
        self.server_url = f'http://127.0.0.1:{self.server_port}'
        self.started = []

    def run(self, args, *, capture=False, env=None):
        print('+ ' + shlex.join(map(str, args)), flush=True)
        return subprocess.run(list(map(str, args)), cwd=ROOT, check=True,
                              stdout=subprocess.PIPE if capture else None,
                              text=True, env=env).stdout

    def kubectl(self, *args, capture=False):
        return self.run(['limactl', 'shell', self.control, 'sudo', 'k3s', 'kubectl', *args], capture=capture)

    def image(self):
        mini = Path(os.environ.get('MINI_OS', '../../mini_os')).resolve()
        if not (mini / 'Makefile').is_file():
            raise RuntimeError(f'Mini OS source not found at {mini}; set MINI_OS=/path/to/mini_os')
        source = fingerprint([(name, ROOT / name) for name in (
            'remote_desktop', 'containers', 'cmd', 'internal', 'guest', 'go.mod', 'tools/build_mini_os.py', 'tools/image_context.py')]
                             + [('mini_os', mini)])
        image = 'docker.io/library/mini-functions:pool-' + source[:16]
        receipt = {'source': source, 'image': image, 'nodes': self.nodes, 'control': self.control}
        stamp = self.state / 'image.json'
        if stamp.exists() and json.loads(stamp.read_text()) == receipt:
            print(f'Image unchanged: {image}', flush=True)
            return image
        image_tar = self.state / ('image-' + source[:16] + '.tar')
        # Lima's Ubuntu /tmp may be tmpfs. Large archives must live on disk.
        guest_dir = '/var/tmp/mini-pool-' + source[:16]
        # Keep completed archives across failed imports. Partial copies never
        # become cache hits; the rename happens only after limactl succeeds.
        if not image_tar.exists():
            self.build_archive(mini, source, image, guest_dir, image_tar)
        for node in self.nodes:
            imported = self.state / ('import-' + hashlib.sha256(node.encode()).hexdigest()[:12] + '.json')
            node_receipt = {'node': node, 'source': source}
            if imported.exists() and json.loads(imported.read_text()) == node_receipt:
                print(f'Image already imported on {node}', flush=True)
                continue
            # Restore the cached archive if the guest staging file was removed.
            self.run(['limactl', 'copy', image_tar, f'{node}:{guest_dir}.tar'])
            self.run(['limactl', 'shell', node, 'sudo', 'k3s', 'ctr', 'images', 'import', '--local', guest_dir + '.tar'])
            imported.write_text(json.dumps(node_receipt))
        stamp.write_text(json.dumps(receipt))  # Only cache after every import succeeds.
        return image

    def build_archive(self, mini, source, image, guest_dir, image_tar):
        with tempfile.TemporaryDirectory(prefix='image-', dir=self.state) as work:
            work = Path(work)
            context = work / 'context'
            self.run([self.python, 'tools/image_context.py', mini, context])
            archive = work / 'context.tgz'
            with tarfile.open(archive, 'w:gz') as tar:
                for entry in sorted(context.iterdir()):
                    tar.add(entry, arcname=entry.name)
            self.run(['limactl', 'copy', archive, f'{self.control}:{guest_dir}.tgz'])
            commands = [
                'set -eu',
                'mkdir -p ' + shlex.quote(guest_dir),
                f'tar -xzf {shlex.quote(guest_dir + ".tgz")} -C {shlex.quote(guest_dir)}',
                shlex.join(['sudo', 'env', 'TMPDIR=/var/tmp', 'buildah', '--storage-driver', 'vfs', 'bud', '--isolation', 'chroot',
                            '--format', 'docker', '-f', guest_dir + '/containers/Containerfile', '-t', image, guest_dir]),
                shlex.join(['sudo', 'env', 'TMPDIR=/var/tmp', 'buildah', '--storage-driver', 'vfs', 'push', image,
                            'docker-archive:' + guest_dir + '.tar']),
                'sudo chmod 644 ' + shlex.quote(guest_dir + '.tar'),
            ]
            self.run(['limactl', 'shell', self.control, 'bash', '-lc', '\n'.join(commands)])
            copied = work / 'image.tar'
            self.run(['limactl', 'copy', f'{self.control}:{guest_dir}.tar', copied])
            copied.replace(image_tar)

    def deploy(self):
        image = self.image()
        # Keep the benchmark's five hosting servers stable during the ramp.
        self.kubectl('-n', 'mini-functions', 'delete', 'hpa', 'mini-functions-worker', '--ignore-not-found')
        manifest = self.state / 'deployment.yaml'
        manifest.write_text((ROOT / self.manifest).read_text().replace('mini-functions:local', image))
        self.run(['limactl', 'copy', manifest, f'{self.control}:/var/tmp/mini-pool-deployment.yaml'])
        self.kubectl('apply', '-f', '/var/tmp/mini-pool-deployment.yaml')
        for name in (('router',) if self.servers == 1 else ('worker', 'router')):
            self.kubectl('-n', 'mini-functions', 'rollout', 'status',
                         'deployment/mini-functions-' + name, '--timeout=300s')

    def token(self):
        encoded = self.kubectl('-n', 'mini-functions', 'get', 'secret', 'mini-functions-auth',
                               '-o', 'jsonpath={.data.token}', capture=True).strip()
        token = base64.b64decode(encoded, validate=True).decode()
        if len(token) < 32:
            raise RuntimeError('Cluster token is missing or too short')
        return token

    def owned_pid(self, name):
        path = self.state / (name + '.json')
        if not path.exists():
            return None
        saved = json.loads(path.read_text())
        result = subprocess.run(['ps', '-p', str(saved['pid']), '-o', 'command='], capture_output=True, text=True)
        if result.returncode == 0 and saved['marker'] in result.stdout:
            return saved['pid']
        return None

    def stop(self, name):
        pid = self.owned_pid(name)
        if pid:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(100):
                if not self.owned_pid(name):
                    break
                time.sleep(.1)
            else:
                os.killpg(pid, signal.SIGKILL)
        (self.state / (name + '.json')).unlink(missing_ok=True)

    def start(self, name, args, port, url, *, token=None, env=None, marker):
        record = self.state / (name + '.json')
        if self.owned_pid(name) and json.loads(record.read_text()).get('args') != args:
            raise RuntimeError(f'{name} settings changed; run make stop before starting it again')
        if not self.owned_pid(name):
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    raise RuntimeError(f'Port {port} is already in use by an unmanaged process; stop the old {name} with Ctrl+C')
            with (self.state / (name + '.log')).open('w') as log:
                process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            record.write_text(json.dumps({'pid': process.pid, 'marker': marker, 'args': args}))
            self.started.append(name)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not self.owned_pid(name):
                raise RuntimeError(f'{name} exited; see {self.state / (name + ".log")}')
            try:
                request = Request(url + '/metrics', headers={'Authorization': 'Bearer ' + token} if token else {})
                with urlopen(request, timeout=2) as response:
                    stats = json.load(response)
                if name == 'router':
                    workers = [w for w in stats.get('workers', []) if w.get('ready')]
                    if len(workers) != self.servers or len({w['node'] for w in workers}) != self.servers:
                        time.sleep(.5)
                        continue
                if stats.get('pending', 0):
                    raise RuntimeError('Server still has pending work; run make stop then make test')
                print(f'{name} ready: {url}', flush=True)
                return
            except (OSError, ValueError):
                time.sleep(.5)
        raise RuntimeError(f'{name} not ready after 90s; see {self.state / (name + ".log")}')

    def router(self):
        self.deploy()
        token = self.token()
        # A rollout can remove the pod selected by an existing port-forward.
        # Reconnect to the current deployment rather than retaining that tunnel.
        self.stop('router')
        command = f'ulimit -n 16384\nexec k3s kubectl -n mini-functions port-forward --address=127.0.0.1 service/mini-functions-router {self.router_port}:8080'
        self.start('router', ['limactl', 'shell', self.control, 'sudo', 'bash', '-lc', command],
                   self.router_port, self.router_url, token=token, marker='port-forward')
        return token

    def server(self):
        # Remove the old frontend; guests execute directly on the five services.
        self.stop('server')
        self.router()
        print(f'{self.servers} warm worker servers ready; direct invocation through ' + self.router_url, flush=True)

    def load(self):
        args = [self.python, '-m', 'tools.load_test_user_rate', '--url', self.router_url,
                '--script', os.environ.get('SCRIPT', 'examples/HELLO.MOS'), '--servers', str(self.servers)]
        for flag, key, default in [('start-users', 'START_USERS', '4096'), ('target-users', 'TARGET_USERS', '1000000'),
                                   ('requests-per-user-minute', 'REQUESTS_PER_MINUTE', '1'), ('duration', 'DURATION', '15'),
                                   ('max-inflight', 'MAX_INFLIGHT', '8192'), ('max-error-fraction', 'MAX_ERROR_FRACTION', '0.01'),
                                   ('memory', 'MEMORY', '8'), ('cpus', 'CPUS', '0.25'), ('job-timeout', 'JOB_TIMEOUT', '30'),
                                   ('request-timeout', 'REQUEST_TIMEOUT', '40'), ('output', 'OUTPUT', 'load-worker-pool.json')]:
            args.extend(['--' + flag, os.environ.get(key, default)])
        self.run(args, env={**os.environ, 'FUNCTION_TOKEN': self.token(), 'PYTHONUNBUFFERED': '1'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('image', 'deploy', 'router', 'server', 'load', 'test', 'stop', 'logs'))
    args = parser.parse_args()
    os.chdir(ROOT)
    setup = Setup()
    succeeded = False
    try:
        if args.action not in {'image', 'deploy', 'stop', 'logs'}:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = 16384 if hard == resource.RLIM_INFINITY else min(16384, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, target), hard))
        if args.action == 'test':
            setup.server()
            setup.load()
        elif args.action == 'stop':
            for name in ('server', 'router'):
                setup.stop(name)
        elif args.action == 'logs':
            for name in ('router', 'server'):
                path = setup.state / (name + '.log')
                print(f'\n{path}:\n' + (path.read_text()[-12000:] if path.exists() else '(not started)'))
        else:
            getattr(setup, args.action)()
        succeeded = True
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Setup failed: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        # Individually started services persist. A full run owns and cleans up
        # only services it started; existing managed services are left running.
        if args.action == 'test' or not succeeded:
            for name in reversed(setup.started):
                setup.stop(name)
    return 0


if __name__ == '__main__':
    sys.exit(main())
