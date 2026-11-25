"""Load llama-cpp-python on Windows without installing the CUDA Toolkit.

The prebuilt CUDA wheel's llama.dll needs cudart64_12.dll, cublas64_12.dll and cublasLt64_12.dll. They normally
come from the CUDA Toolkit. Setup installs them from pip instead (nvidia-cuda-runtime-cu12, nvidia-cublas-cu12),
into site-packages/nvidia/*/bin, and this module makes those folders findable before llama_cpp is imported.
llama-cpp-python loads its DLL with the standard search order, so the folders go on PATH as well as through
os.add_dll_directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False


def dll_dirs() -> list[Path]:
    """Folders that may hold the CUDA runtime DLLs, most specific first."""
    dirs: list[Path] = []
    for entry in sys.path:
        nv = Path(entry or ".") / "nvidia"
        if nv.is_dir():
            dirs += sorted(p for p in nv.glob("*/bin") if p.is_dir())
    for var in ("CUDA_PATH", "CUDA_PATH_V12_4"):
        if os.environ.get(var):
            dirs.append(Path(os.environ[var]) / "bin")
    seen: set[str] = set()
    out = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


def prepare() -> list[Path]:
    """Make the CUDA DLL folders visible to the loader. Safe to call often; does nothing off Windows."""
    global _done
    if sys.platform != "win32":
        return []
    dirs = dll_dirs()
    if _done:
        return dirs
    _done = True
    path = os.environ.get("PATH", "")
    for d in dirs:
        try:
            os.add_dll_directory(str(d))
        except (OSError, AttributeError):
            pass
        if str(d).lower() not in path.lower():
            path = str(d) + os.pathsep + path
    os.environ["PATH"] = path
    return dirs


FIX = ("The model runner is installed, but Windows can't load it because a DLL it needs is missing.\n"
       "            Install the CUDA runtime from pip (no CUDA Toolkit needed):\n"
       "            .\\.venv\\Scripts\\python -m pip install \"nvidia-cuda-runtime-cu12==12.4.*\" \"nvidia-cublas-cu12==12.4.*\"\n"
       "            If it still fails, install the Microsoft Visual C++ 2015-2022 Redistributable (x64):\n"
       "            https://aka.ms/vs/17/release/vc_redist.x64.exe  then reopen PowerShell")


def import_llama():
    """Import llama_cpp after preparing the DLL folders. Raises ImportError with a plain fix on failure."""
    prepare()
    try:
        import llama_cpp
    except ImportError:
        raise
    except Exception as e:  # llama-cpp-python raises RuntimeError when llama.dll can't load
        raise ImportError(f"{e}\n{FIX}") from e
    return llama_cpp
