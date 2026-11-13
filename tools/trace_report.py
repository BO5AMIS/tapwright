"""Print the timing trace's table: python tools/trace_report.py [FILE]  (default ~/.tapwright/trace.jsonl).
Same as `python -m tapwright.trace`."""
import sys

from tapwright.trace import main

if __name__ == "__main__":
    sys.exit(main())
