"""Create a clean OCI build context containing Mini OS and this application."""
import argparse
from pathlib import Path
import shutil


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mini_os", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    repository = Path(__file__).resolve().parents[1]
    shutil.copytree(args.mini_os, args.output / "mini_os", ignore=shutil.ignore_patterns(".git", "out", "*.img", "kernel.elf", "kernel.bin"))
    for directory in ("remote_desktop", "tools", "containers"):
        shutil.copytree(repository / directory, args.output / directory, ignore=shutil.ignore_patterns("__pycache__"))
