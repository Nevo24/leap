#!/usr/bin/env python3
"""Verify Claude compacting-conversation detection in a Leap session.

Usage:
    python src/scripts/leap-verify-compact.py [tag]

If ``tag`` is omitted, the most recently modified state log under
``.storage/state_logs/`` is analysed.

Scans the tracker's debug log for the three transition paths that can
fire during conversation compaction and reports which one caught it:

  - new indicator path       (my fix: on_output pattern match)
  - signal-idle suppression  (my fix: Stop-hook ignored while compacting)
  - cursor auto-resume       (pre-existing cursor-hidden fallback)

Prints a verdict so you don't have to read the log by hand.
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_LOG_DIR = PROJECT_ROOT / ".storage" / "state_logs"


MARKERS = {
    "indicator": "running indicator on screen",        # idle→running via pattern
    "suppress": "signal=idle but running indicator",   # Stop hook suppressed
    "cursor":   "cursor hidden at poll, auto-resume",  # pre-existing fallback
    "silence":  "cursor visible + output silent",      # running→idle fallback
    "safety":   "safety timeout",                       # silence timeout
}


def pick_log(tag: str | None) -> Path | None:
    if tag:
        p = STATE_LOG_DIR / f"{tag}.log"
        return p if p.exists() else None
    if not STATE_LOG_DIR.is_dir():
        return None
    logs = sorted(STATE_LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def count_markers(log: Path) -> dict[str, int]:
    counts = {k: 0 for k in MARKERS}
    with open(log) as f:
        for line in f:
            for key, needle in MARKERS.items():
                if needle in line:
                    counts[key] += 1
    return counts


def verdict(counts: dict[str, int]) -> str:
    ind = counts["indicator"] + counts["suppress"]
    cursor = counts["cursor"]
    wrongful_idle = counts["silence"] + counts["safety"]
    if ind > 0:
        msg = "[OK] indicator-based fix fired — compacting detection works"
        if cursor > 0:
            msg += " (cursor fallback also fired, harmless)"
        return msg
    if cursor > 0:
        return (
            "[INFO] only cursor-based auto-resume fired — the indicator "
            "pattern never matched. Either no compaction happened in this "
            "session, or pyte didn't render the spinner as a contiguous "
            '"Compacting conversation" substring. The fix is a no-op here.'
        )
    if wrongful_idle > 0:
        return (
            "[WARN] idle fallback fired and no compact-indicator events — "
            "if you saw 'Compacting conversation…' on screen during this "
            "session, the state likely went to idle. Pattern did not match."
        )
    return "[INFO] no compaction-related transitions in log — probably no compact happened."


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else None
    log = pick_log(tag)
    if log is None:
        where = f"tag={tag}" if tag else STATE_LOG_DIR
        print(f"No state log found for {where}", file=sys.stderr)
        return 1

    counts = count_markers(log)
    print(f"log: {log}")
    print()
    print("events:")
    print(f"  idle→running via pattern (new fix):         {counts['indicator']}")
    print(f"  stop-hook suppressed while compacting (new): {counts['suppress']}")
    print(f"  idle→running via cursor hidden (existing):  {counts['cursor']}")
    print(f"  running→idle via cursor+silence (fallback): {counts['silence']}")
    print(f"  running→idle via safety timeout (fallback): {counts['safety']}")
    print()
    print(verdict(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
