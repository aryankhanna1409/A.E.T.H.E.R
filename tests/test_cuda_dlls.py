"""The CUDA runtime DLL helper: llama.dll must load on Windows without the CUDA Toolkit."""
import os
import sys

import pytest

from aether.llm import cuda_dlls


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(cuda_dlls, "_done", False)
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.delenv("CUDA_PATH_V12_4", raising=False)


def make_nvidia(root):
    for pkg in ("cuda_runtime", "cublas"):
        (root / "nvidia" / pkg / "bin").mkdir(parents=True)
    (root / "nvidia" / "cuda_runtime" / "include").mkdir()
    return root


def test_finds_pip_cuda_bin_folders(tmp_path, monkeypatch):
    site = make_nvidia(tmp_path / "site")
    monkeypatch.setattr(sys, "path", [str(site)])
    names = [d.parent.name for d in cuda_dlls.dll_dirs()]
    assert names == ["cublas", "cuda_runtime"]


def test_cuda_path_is_used_and_duplicates_dropped(tmp_path, monkeypatch):
    (tmp_path / "toolkit" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [str(tmp_path), str(tmp_path)])
    monkeypatch.setenv("CUDA_PATH", str(tmp_path / "toolkit"))
    monkeypatch.setenv("CUDA_PATH_V12_4", str(tmp_path / "toolkit"))
    assert cuda_dlls.dll_dirs() == [tmp_path / "toolkit" / "bin"]


def test_prepare_does_nothing_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    before = os.environ.get("PATH")
    assert cuda_dlls.prepare() == []
    assert os.environ.get("PATH") == before


def test_prepare_on_windows_puts_folders_first_on_path_once(tmp_path, monkeypatch):
    site = make_nvidia(tmp_path / "site")
    monkeypatch.setattr(sys, "path", [str(site)])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", "WINDIR")
    added = []
    monkeypatch.setattr(os, "add_dll_directory", added.append, raising=False)
    dirs = cuda_dlls.prepare()
    cuda_dlls.prepare()
    assert added == [str(d) for d in dirs] and len(dirs) == 2
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[-1] == "WINDIR" and len(parts) == 3


def write_fake_llama(root, body):
    pkg = root / "llama_cpp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(body)
    return root


def test_dll_load_failure_becomes_import_error_with_the_fix(tmp_path, monkeypatch):
    root = write_fake_llama(tmp_path, "raise RuntimeError(\"Failed to load shared library 'llama.dll'\")\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    with pytest.raises(ImportError) as e:
        cuda_dlls.import_llama()
    assert "llama.dll" in str(e.value) and "nvidia-cuda-runtime-cu12" in str(e.value)
    assert not isinstance(e.value, ModuleNotFoundError)


def test_not_installed_stays_module_not_found(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    with pytest.raises(ImportError):
        cuda_dlls.import_llama()


def test_provider_reports_a_broken_runner_plainly(tmp_path, monkeypatch):
    from aether.core.config import LLMConfig
    from aether.llm.base import LLMUnavailable
    from aether.llm.llama_cpp_provider import LlamaCppProvider
    root = write_fake_llama(tmp_path, "raise RuntimeError('Could not find module llama.dll')\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    with pytest.raises(LLMUnavailable) as e:
        LlamaCppProvider(LLMConfig(model_path=str(tmp_path / "m.gguf")))
    assert "nvidia-cublas-cu12" in str(e.value)


def test_doctor_shows_the_real_import_error(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("doctor", os.path.join(os.path.dirname(__file__), "..", "scripts", "doctor.py"))
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    write_fake_llama(tmp_path, "")
    (tmp_path / "brokenvoice").mkdir()
    (tmp_path / "brokenvoice" / "__init__.py").write_text("raise OSError('av.dll not found')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert not doctor.has("brokenvoice") and not doctor.has("surely_not_a_module_xyz")
    assert doctor.why(["brokenvoice", "surely_not_a_module_xyz"]) == \
        "brokenvoice (OSError: av.dll not found); surely_not_a_module_xyz (not installed)"
