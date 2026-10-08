"""Find the user count where p95 queue delay reaches five seconds."""
from tools.function_load_common import main

if __name__ == "__main__":
    main(threshold_seconds=5)
