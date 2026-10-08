"""Build a fail-fast Mini OS overlay without changing the source repository."""
from pathlib import Path
import argparse
import re
import shutil
import subprocess


def overlay_block_timeout(root):
    """Use elapsed guest time rather than CPU-speed-dependent polling counts."""
    path = root / "src/mmio.c"
    text = path.read_text()
    old = "    wait = 0;\n    while (vq_used->idx == driver_used_idx) {\n        if (++wait == DISK_WAIT_LIMIT) return -2;\n    }"
    if text.count(old) != 1 or text.count("    uint32_t wait;") != 1:
        raise RuntimeError("Mini OS block I/O interface changed")
    text = text.replace("    uint32_t wait;", "    uint64_t wait_started, wait_frequency;")
    text = text.replace(old, '''    __asm__ volatile("mrs %0, cntpct_el0" : "=r"(wait_started));
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(wait_frequency));
    while (vq_used->idx == driver_used_idx) {
        uint64_t current;
        __asm__ volatile("mrs %0, cntpct_el0" : "=r"(current));
        if (current - wait_started >= wait_frequency * 5) return -2;
    }''')
    path.write_text(text)


def overlay(root):
    overlay_block_timeout(root)
    commands = root / "src/commands.c"
    text = commands.read_text()
    # Existing error branches print diagnostics but return success. Make all
    # command diagnostics in execute() return -1, including one-line branches.
    head, body = text.split("int execute(char* cmd) {", 1)
    diagnostic = r'printstr\((?:"[^"\n]*"|append \? "[^"\n]*" : "[^"\n]*")\);'
    def failure(match):
        statement = match.group(0)
        if not any(word in statement for word in ("usage:", "failed", "too long", "not provided", "invalid", "Invalid command")):
            return statement
        return "{ " + statement + " return -1; }"
    body, n = re.subn(diagnostic, failure, body)
    if n < 20:
        raise RuntimeError("Mini OS command interface changed; review the fail-fast overlay")
    body = body.replace("void *mem = malloc(n);", 'void *mem = malloc(n);\n        if (!mem) { printstr("\\nalloc: out of memory"); return -1; }')
    commands.write_text(head + "int execute(char* cmd) {" + body)
    script = root / "src/program/script.c"
    text = script.read_text()
    old = "if (!p->c->error && execute(cmd)) p->c->stopped = 1;"
    if text.count(old) != 1:
        raise RuntimeError("Mini OS shell interface changed")
    text = text.replace(old, 'if (!p->c->error) { int status = execute(cmd);\n        if (status < 0) fail(p, "shell command failed");\n        else if (status) p->c->stopped = 1; }')
    old = "expect(p, ')'); if (run && !p->c->error && tick(p)) v = call(p, id, args, argc);"
    if text.count(old) != 1:
        raise RuntimeError("Mini OS builtin interface changed")
    checks = " || ".join(f'equal(id, "{name}")' for name in (
        "open", "read", "write", "close", "mkdir", "remove", "rmdir",
        "tcp_connect", "tcp_send", "tcp_recv", "tcp_close",
    ))
    text = text.replace(old, old + f'\n            if (run && v.type == NUMBER && v.number < 0 && ({checks})) fail(p, "I/O operation failed");')
    script.write_text(text)
    # A failed exec must power off, with an unambiguous host-observable status.
    kernel = root / "kernel.c"
    text = kernel.read_text()
    old = 'if (done) { printstr("\\nShutting down...\\n"); return; }'
    if text.count(old) != 1:
        raise RuntimeError("Mini OS shutdown interface changed")
    text = text.replace(old, 'if (done < 0) { printstr("\\nMINI_JOB_FAILED\\n"); return; }\n            ' + old)
    kernel.write_text(text)


def build(source, output, *, cc="aarch64-elf-gcc", assembler="aarch64-elf-as", linker="aarch64-elf-ld", protocol=1):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("build output must be a new directory")
    shutil.copytree(source, output, ignore=shutil.ignore_patterns(".git", "out", "kernel.elf", "kernel.bin", "*.img"))
    overlay(output)
    if protocol == 2:
        shutil.copyfile(Path(__file__).resolve().parents[1] / "guest/job.c", output / "kernel.c")
        io = output / "src/io.c"
        io.write_text(io.read_text().replace("uart_put(c);", "extern void job_printchar(char); job_printchar(c);"))
        script = output / "src/program/script.c"
        text = script.read_text()
        anchor = '#define DONE }'
        text = text.replace(anchor, anchor + '''
    BUILTIN("fanout", 2)
        extern int job_fanout(const char *, int);
        int reverse = args[0].type == NUMBER;
        char *name = text(p, args[reverse ? 1 : 0]); int64_t n = numeric(p, args[reverse ? 0 : 1]);
        if (c->error || n < 1 || n > 256 || job_fanout(name, (int)n)) fail(p, "invalid fanout");
        return number(0);
    DONE
''')
        script.write_text(text)
    subprocess.run(["make", "-C", str(output), "kernel.elf", "out/mkfat32", f"CC={cc}", f"AS={assembler}", f"LD={linker}"], check=True)
    if protocol == 1:
        (output / "job-runtime-v1").write_text("fail-fast\n")
    if protocol == 2:
        (output / "job-runtime-v2").write_text("framed-uart-single-use\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cc", default="aarch64-elf-gcc")
    parser.add_argument("--assembler", default="aarch64-elf-as")
    parser.add_argument("--linker", default="aarch64-elf-ld")
    parser.add_argument("--protocol", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    build(args.source, args.output, cc=args.cc, assembler=args.assembler, linker=args.linker, protocol=args.protocol)
