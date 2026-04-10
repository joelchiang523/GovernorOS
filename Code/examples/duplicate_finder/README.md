# Duplicate File Finder — GovernorOS Example

A tkinter GUI application that finds duplicate files by SHA-256 hash.

Built by GovernorOS (qwen3.5:27b via Aider) in **1 iteration**, all 4 harness tests passing.

## Features

- Browse and select a scan directory
- Recursive SHA-256 scan to identify duplicates
- Treeview display with colour-coded duplicate groups
- Checkbox selection for files to delete
- Confirmation dialog before deletion
- `find_duplicates(path)` is importable independently for testing

## Run

```bash
python main.py
```

## Test

```bash
python -m pytest tests/ -q
```

## Harness

The `tests/test_harness.py` file was the acceptance criteria used by GovernorOS:

1. `main.py` exists
2. `main.py` has no syntax errors
3. `find_duplicates` is importable without error
4. `find_duplicates(path)` correctly identifies duplicate files
