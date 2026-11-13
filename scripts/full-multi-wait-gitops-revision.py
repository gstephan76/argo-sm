#!/usr/bin/env python3
"""Compatibility entry point for the protected-migration synchronization gate."""
import os
from pathlib import Path
import sys

bootstrap = Path(__file__).with_name('full-multi-bootstrap.py')
os.execv(sys.executable, [sys.executable, str(bootstrap), 'wait-sync',
                         '--allow-legacy-prune-disabled', *sys.argv[1:]])
