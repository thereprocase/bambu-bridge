#!/usr/bin/env python3
"""Compatibility entry point for the Gridline vector export pipeline."""
import pathlib
import subprocess
import sys

if len(sys.argv) > 1:
    raise SystemExit("Gridline assets use assets/brand/*.svg. Run node scripts/build-brand.cjs.")
subprocess.run(["node", str(pathlib.Path(__file__).with_name("build-brand.cjs"))], check=True)
