"""
Project Understanding Agent.
Scans a repo root, classifies files (RTL vs testbench vs other), and guesses the top module.
"""
from __future__ import annotations
import os
from core.schemas import ProjectManifest


def scan_project(root: str, top_module_hint: str | None = None) -> ProjectManifest:
    rtl_files, tb_files = [], []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith((".v", ".sv")):
                full = os.path.join(dirpath, fn)
                if "tb" in dirpath.lower() or fn.lower().startswith("tb_") or "_tb" in fn.lower():
                    tb_files.append(full)
                else:
                    rtl_files.append(full)

    top_module = top_module_hint
    return ProjectManifest(
        root=root, rtl_files=sorted(rtl_files), tb_files=sorted(tb_files),
        top_module=top_module, toolchain="icarus",
    )
