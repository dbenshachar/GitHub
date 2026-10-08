"""Measured Linux host/cgroup headroom for admission, without execution slots."""
import os
from pathlib import Path
import time


class Resources:
    def __init__(self, cpu_budget, memory_budget_mb, *, interval=.1):
        self.cpu_budget, self.memory_budget = cpu_budget, memory_budget_mb
        self.interval = interval
        self.previous = None
        self.value = None

    def snapshot(self):
        now = time.monotonic()
        if self.previous and now - self.previous[0] < self.interval:
            return self.value
        cgroup = Path('/sys/fs/cgroup')
        cpu_used = node_cpu_used = 0.0
        cpu_limit = self.cpu_budget
        memory_available = self.memory_budget
        node_cores = os.cpu_count() or 1
        usage = total = idle = 0
        try:
            stat = dict(line.split() for line in (cgroup / 'cpu.stat').read_text().splitlines())
            usage = int(stat['usage_usec'])
            quota, period = (cgroup / 'cpu.max').read_text().split()
            if quota != 'max':
                cpu_limit = min(cpu_limit, int(quota) / int(period))
            limit = (cgroup / 'memory.max').read_text().strip()
            if limit != 'max':
                memory_available = min(memory_available,
                    (int(limit) - int((cgroup / 'memory.current').read_text())) / 1048576 - 32)
        except (OSError, KeyError, ValueError):
            pass
        try:
            fields = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
            total, idle = sum(fields), fields[3] + fields[4]
            memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
            memory_available = min(memory_available, int(memory['MemAvailable'].split()[0]) / 1024 - 128)
        except (OSError, KeyError, ValueError):
            pass
        if self.previous:
            elapsed = max(.001, now - self.previous[0])
            cpu_used = max(0, usage - self.previous[1]) / 1e6 / elapsed
            delta = total - self.previous[2]
            if delta > 0:
                node_cpu_used = node_cores * (1 - (idle - self.previous[3]) / delta)
        self.previous = (now, usage, total, idle)
        self.value = {'cpu_used_cores': cpu_used, 'cpu_limit_cores': cpu_limit,
                      'cpu_available_cores': max(0, min(cpu_limit - cpu_used, node_cores - node_cpu_used)),
                      'node_cpu_used_cores': node_cpu_used,
                      'memory_available_mb': max(0, memory_available), 'sample_monotonic': now}
        return self.value
