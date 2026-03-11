#!/usr/bin/env python3
"""Thin launcher for the Leap Slack bot."""

import os
import sys

# Add src directory to path for imports
src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from leap.slack.bot import main

if __name__ == '__main__':
    main()
