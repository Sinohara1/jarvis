#!/usr/bin/env python3
"""Джарвис — personal voice AI desk assistant. Run: pythonw jarvis.pyw"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jarvis_app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
