"""scripts/backend_entry.py

PyInstaller entry point — just re-exports the real CLI.

This file exists so that PyInstaller can build a spec that clearly
declares the backend entry point and hidden imports (pdfium, mcp).
"""
from __future__ import annotations

from mynotes.__main__ import main

if __name__ == "__main__":
    main()
