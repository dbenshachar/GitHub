"""Ramp from 4,096 toward one million continuously requesting users."""
from tools.function_load_common import main

if __name__ == "__main__":
    main(threshold_seconds=5, target_users=1_000_000, start_users=4096)
