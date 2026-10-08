"""Build and supervise the Go/Mini OS service directly in one Lima VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
from tools.load_test_setup import fingerprint

ROOT = Path(__file__).resolve().parents[1]


class Standalone:
    def __init__(self):
        self.vm = os.environ.get('VM', 'fc')
        if not self.vm.replace('-', '').replace('_', '').isalnum():
            raise ValueError('Invalid VM name')
        self.state = ROOT / '.load-test' / ('standalone-' + self.vm)
        self.state.mkdir(parents=True, exist_ok=True)
        self.token_file = self.state / 'token'
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_hex(32))
            self.token_file.chmod(0o600)
        self.token = self.token_file.read_text().strip()
        self.base = '/var/tmp/mini-functions-go'
        self.port = int(os.environ.get('ROUTER_PORT', '8080'))
        if not 1 <= self.port <= 65535:
            raise ValueError('Invalid router port')

    def run(self, args, capture=False, env=None):
        print('+ ' + shlex.join(map(str, args)), flush=True)
        return subprocess.run(list(map(str, args)), cwd=ROOT, check=True, text=True,
                              stdout=subprocess.PIPE if capture else None, env=env).stdout

    def shell(self, command, capture=False):
        return self.run(['limactl', 'shell', self.vm, 'bash', '-lc', command], capture)

    def bootstrap(self):
        # Explicit bootstrap changes only the selected VM, never other instances.
        names = self.run(['limactl', 'list', '--format', '{{.Name}}'], True).split()
        if self.vm not in names:
            self.run(['limactl', 'start', '--name', self.vm, str(ROOT / 'fc.yaml')])
        else:
            self.run(['limactl', 'stop', self.vm])
            self.run(['limactl', 'edit', '--tty=false', self.vm, '--cpus', '4', '--memory', '8',
                      '--nested-virt', '--network', 'vzNAT'])
            self.run(['limactl', 'start', self.vm])
        self.shell('sudo apt-get update\nsudo apt-get install -y qemu-system-arm make gcc binutils-aarch64-linux-gnu gcc-aarch64-linux-gnu')
        self.url()

    def image(self):
        mini = Path(os.environ.get('MINI_OS', '../../mini_os')).resolve()
        sources = [(name, ROOT / name) for name in ('go.mod', 'cmd', 'internal', 'guest', 'tools/build_mini_os.py')]
        source = fingerprint(sources + [('mini_os', mini)])[:16]
        dest = self.base + '/' + source
        # Check remote artifacts too: a stopped/recreated VM cannot use a stale local receipt.
        exists = self.shell(f'test -x {dest}/mini-functions && test -f {dest}/runtime/job-runtime-v2 && echo ready || true', True)
        if exists.strip() == 'ready':
            (self.state / 'build.json').write_text(json.dumps({'source': source, 'path': dest}))
            print('Go runtime unchanged: ' + source, flush=True)
            return dest
        arch = self.shell('uname -m', True).strip()
        if arch not in ('aarch64', 'arm64'):
            raise RuntimeError('Mini OS standalone profile requires an ARM64 Lima VM')
        try:
            self.shell('command -v qemu-system-aarch64 && command -v make && command -v aarch64-linux-gnu-gcc')
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('Missing guest build dependencies. Run make bootstrap VM=' + self.vm) from exc
        with tempfile.TemporaryDirectory(dir=self.state) as tmp:
            tmp = Path(tmp)
            binary = tmp / 'mini-functions'
            self.run(['go', 'build', '-trimpath', '-o', binary, './cmd/mini-functions'], env={**os.environ,
                'GOOS': 'linux', 'GOARCH': 'arm64', 'CGO_ENABLED': '0', 'GOCACHE': str(ROOT / '.go-cache')})
            archive = tmp / 'source.tgz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(mini, arcname='mini_os', filter=lambda member: None if any(
                    part in {'.git', 'out', '__pycache__'} for part in Path(member.name).parts)
                    or member.name.endswith(('.img', 'kernel.elf', 'kernel.bin')) else member)
                tar.add(ROOT / 'guest', arcname='guest')
                tar.add(ROOT / 'tools/build_mini_os.py', arcname='tools/build_mini_os.py')
            self.shell('mkdir -p ' + dest)
            self.run(['limactl', 'copy', archive, self.vm + ':' + dest + '/source.tgz'])
            self.run(['limactl', 'copy', binary, self.vm + ':' + dest + '/mini-functions'])
            self.shell(f'set -eu\ncd {dest}\ntar -xzf source.tgz\nchmod +x mini-functions\n'
                       'python3 tools/build_mini_os.py mini_os runtime --protocol 2 '
                       '--cc aarch64-linux-gnu-gcc --assembler aarch64-linux-gnu-as --linker aarch64-linux-gnu-ld')
        (self.state / 'build.json').write_text(json.dumps({'source': source, 'path': dest}))
        return dest

    def url(self):
        interfaces = json.loads(self.shell('ip -j -4 addr show', True))
        addresses = [address['local'] for interface in interfaces for address in interface.get('addr_info', [])
                     if address.get('family') == 'inet' and address.get('local', '').startswith('192.168.64.')]
        ip = addresses[0] if addresses else ''
        if not ip:
            raise RuntimeError('Add networks: [{vzNAT: true}] to the VM Lima config and restart it; this profile uses direct host-to-guest networking')
        return f'http://{ip}:{self.port}'

    def server(self):
        dest = self.image()
        # Token contents never appear in the command line or logs.
        self.run(['limactl', 'copy', self.token_file, self.vm + ':' + dest + '/token'])
        self.shell(f'chmod 600 {dest}/token')
        if os.environ.get("ACCEL", "auto") not in {"auto", "tcg", "kvm"}:
            raise ValueError("ACCEL must be auto, tcg, or kvm")
        command = f'exec {dest}/mini-functions --mini-os-root {dest}/runtime --listen :{self.port} ' \
            f'--accel {shlex.quote(os.environ.get("ACCEL", "auto"))} --cpu-budget 3 --memory-budget 6144 ' \
            f'--pool-min {int(os.environ.get("POOL_MIN", "8"))} --pool-max {int(os.environ.get("POOL_MAX", "64"))} ' \
            f'--calibrate {int(os.environ.get("CALIBRATE", "100"))} --schedule-state {self.base}/schedules.json'
        # A systemd unit gives aggregate CPU/memory ceilings and background logs.
        guest_user = self.shell('id -un', True).strip()
        if not guest_user.replace('_', '').replace('-', '').isalnum():
            raise RuntimeError('Invalid guest user name')
        unit = '[Unit]\nDescription=Mini OS Go supervisor\n[Service]\nType=simple\n' \
            f'User={guest_user}\nNoNewPrivileges=yes\n' \
            f'ExecStart=/bin/bash -lc \'export FUNCTION_TOKEN=$$(cat {dest}/token); {command}\'\n' \
            'CPUQuota=300%\nMemoryMax=6G\nLimitNOFILE=16384\nTimeoutStopSec=15\nKillMode=control-group\n' \
            '[Install]\nWantedBy=multi-user.target\n'
        unit_file = self.state / 'mini-functions-go.service'
        unit_file.write_text(unit)
        self.run(['limactl', 'copy', unit_file, self.vm + ':' + self.base + '/mini-functions-go.service'])
        self.shell(f'sudo cp {self.base}/mini-functions-go.service /etc/systemd/system/mini-functions-go.service\n'
                   'sudo systemctl daemon-reload\nsudo systemctl restart mini-functions-go')
        url = self.url()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urlopen(Request(url + '/metrics', headers={'Authorization': 'Bearer ' + self.token}), timeout=2) as response:
                    metrics = json.load(response)
                print('Supervisor ready at ' + url + '\n' + json.dumps(metrics, indent=2), flush=True)
                return
            except OSError:
                time.sleep(.5)
        raise RuntimeError('Supervisor not ready. Run make logs PROFILE=standalone')

    router = server
    deploy = server

    def load(self, qualify=False):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(16384, hard) if hard != resource.RLIM_INFINITY else 16384, hard))
        self.run(['go', 'build', '-o', self.state / 'mini-load', './cmd/mini-load'], env={**os.environ, 'GOCACHE': str(ROOT / '.go-cache')})
        args = [str(self.state / 'mini-load'), '--url', self.url(), '--servers', '1',
                '--script', os.environ.get('SCRIPT', 'examples/HELLO.MOS'), '--threshold-seconds', '1']
        options = {'start-users': os.environ.get('START_USERS', '4096'), 'target-users': os.environ.get('TARGET_USERS', '1000000'),
                   'duration': os.environ.get('DURATION', '15'), 'max-inflight': os.environ.get('MAX_INFLIGHT', '8192'),
                   'output': os.environ.get('OUTPUT', 'load-go.json'), 'requests-per-user-minute': os.environ.get('REQUESTS_PER_MINUTE', '1'),
                   'job-timeout': os.environ.get('JOB_TIMEOUT', '30'), 'request-timeout': os.environ.get('REQUEST_TIMEOUT', '40')}
        if os.environ.get('VERBOSE') == '1':
            args += ['--verbose']
        if qualify:
            users = os.environ.get('QUALIFY_USERS', '1500')
            options.update({'start-users': users, 'target-users': users, 'duration': '600'})
            args += ['--repetitions', '3', '--qualify']
        for flag, value in options.items():
            args += ['--' + flag, value]
        self.run(args, env={**os.environ, 'FUNCTION_TOKEN': self.token})

    def stop(self):
        self.shell('sudo systemctl stop mini-functions-go')

    def logs(self):
        self.shell('sudo journalctl -u mini-functions-go --no-pager -n 100')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('bootstrap', 'image', 'deploy', 'router', 'server', 'load', 'test', 'qualify', 'stop', 'logs'))
    args = parser.parse_args()
    setup = Standalone()
    try:
        if args.action == 'test':
            setup.server()
            setup.load()
        elif args.action == 'qualify':
            setup.server()
            setup.load(True)
        else:
            getattr(setup, args.action)()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Setup failed: {exc}\n')


if __name__ == '__main__':
    main()
