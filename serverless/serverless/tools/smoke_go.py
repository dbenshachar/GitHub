"""Short local API verification; this is not a Lima capacity qualification."""
import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path('.mini-runtime-v2'))
    parser.add_argument('--output', type=Path, default=Path('.load-test/local-api-smoke.json'))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'FUNCTION_TOKEN': secrets.token_hex(32), 'GOCACHE': str(ROOT / '.go-cache')}
    report = {'host': platform.platform(), 'qualification': False, 'tests': [],
              'note': 'Short local Mac/Linux API smoke; not an 8 GB Lima capacity claim.'}
    with tempfile.TemporaryDirectory(dir=args.output.parent) as work:
        work = Path(work)
        for command in ('mini-functions', 'mini-load'):
            subprocess.run(['go', 'build', '-o', str(work / command), './cmd/' + command], cwd=ROOT, env=env, check=True)
        for synthetic in (True, False):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            name = 'control-plane' if synthetic else 'fresh-guests'
            command = [str((work / 'mini-functions').resolve()), '--listen', f'127.0.0.1:{port}']
            command += ['--benchmark-echo'] if synthetic else ['--mini-os-root', str(args.runtime.resolve()),
                '--calibrate', '10', '--pool-min', '8', '--pool-max', '16', '--memory-budget', '1024', '--accel', 'tcg']
            with (work / 'service.log').open('w') as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    ready = False
                    for _ in range(300):
                        if process.poll() is not None:
                            raise RuntimeError((work / 'service.log').read_text())
                        try:
                            with urlopen(url + '/healthz', timeout=.2):
                                ready = True
                                break
                        except OSError:
                            time.sleep(.1)
                    if not ready:
                        raise RuntimeError('Supervisor startup timed out')
                    if not synthetic:
                        cli = subprocess.run(['python3', '-m', 'remote_desktop.function_cli', 'run',
                            'examples/HELLO.MOS', '--router', url, '--memory', '8'], env=env, capture_output=True, text=True)
                        if cli.returncode:
                            raise RuntimeError('Python CLI failed: ' + cli.stderr)
                        print('Python CLI:', json.loads(cli.stdout)['output'].strip(), flush=True)
                    users = '16020' if synthetic else '1500'
                    output = work / 'stage.json'
                    command = [str((work / 'mini-load').resolve()), '--url', url, '--start-users', users,
                        '--target-users', users, '--duration', '3' if synthetic else '5', '--max-inflight', '1024', '--output', str(output)]
                    if synthetic:
                        command += ['--transport-only']
                    subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True)
                    stage = json.loads(output.read_text())['stages'][0]
                    summary = {'mode': name, **{key: stage.get(key) for key in ('offered_rps', 'planned_requests',
                        'completed', 'error_fraction', 'latency_ms', 'pool_hits', 'lifecycle_cpu_ms')}}
                    report['tests'].append(summary)
                    print(json.dumps(summary, indent=2), flush=True)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('Local verification report: ' + str(args.output.resolve()), flush=True)


if __name__ == '__main__':
    main()
