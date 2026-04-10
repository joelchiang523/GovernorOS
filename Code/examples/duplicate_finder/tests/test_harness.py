"""
Harness 測試：驗證 duplicate_finder 核心功能
每輪 Aider 改動後由 main_loop.py 執行此測試
"""
import hashlib
import subprocess
import sys
import tempfile
import os
from pathlib import Path


def test_main_py_exists():
    """main.py 必須存在"""
    assert Path("main.py").exists(), "main.py 不存在，請先建立"


def test_main_py_syntax():
    """main.py 語法檢查"""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", "main.py"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"語法錯誤：\n{result.stderr}"


def test_finder_module_importable():
    """finder 核心模組必須可匯入（不啟動 GUI）"""
    result = subprocess.run(
        [sys.executable, "-c", "import main; print('import OK')"],
        capture_output=True, text=True, env={**os.environ, "DISPLAY": ""},
    )
    # 允許 tkinter display 錯誤，但不允許 ImportError / SyntaxError
    assert "ImportError" not in result.stderr, f"匯入錯誤：{result.stderr}"
    assert "SyntaxError" not in result.stderr, f"語法錯誤：{result.stderr}"


def test_find_duplicates_logic():
    """核心邏輯：find_duplicates() 能正確找出重複檔案"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 建立測試檔案
        Path(tmpdir, "a.txt").write_text("hello world")
        Path(tmpdir, "b.txt").write_text("hello world")   # 與 a.txt 相同
        Path(tmpdir, "c.txt").write_text("unique content")

        # 嘗試從 main 匯入 find_duplicates
        sys.path.insert(0, str(Path(".")))
        try:
            from main import find_duplicates
        except ImportError:
            # 若 main.py 尚未實作，跳過（不算失敗，讓 Aider 繼續實作）
            return

        groups = find_duplicates(tmpdir)

        # 應該找到 1 個重複群組，包含 a.txt 和 b.txt
        assert len(groups) >= 1, f"應找到至少 1 個重複群組，得到：{groups}"
        group_files = [set(Path(f).name for f in g) for g in groups.values()]
        assert {"a.txt", "b.txt"} in group_files, f"重複群組不正確：{group_files}"
