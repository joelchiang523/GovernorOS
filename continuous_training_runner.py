#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
continuous_training_runner.py

系統層訓練 runner：
- 啟動 morning 流程
- 在指定時長內持續執行 benchmark suites / task queue
- 週期性匯出 training data 與狀態摘要
- 結束時執行 evening 流程
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).parent
MAIN_LOOP = BASE_DIR / "main_loop.py"
BENCHMARK_RUNNER = BASE_DIR / "benchmark_runner.py"
DREAM_CYCLE = BASE_DIR / "dream_cycle.py"
STARTUP = BASE_DIR / "startup.sh"
DEFAULT_RUN_ROOT = BASE_DIR / "training_runs"
DEFAULT_AIDER_MODEL = os.environ.get("OLLAMA_MODEL_AIDER", "ollama/qwen3.5:9b")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_jsonl(path: Path, record: dict) -> None:
    ensure_dir(path.parent)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_work_dir(path_str: str) -> str:
    path = Path(path_str).resolve()
    if not path.exists():
        raise ValueError(f"work_dir 不存在：{path}")
    if path == BASE_DIR:
        raise ValueError("work_dir 不能直接指向 AI_system_v3 根目錄")
    return str(path)


def load_task_queue(path: Path) -> list[dict]:
    payload = load_json(path)
    tasks = payload.get("tasks", [])
    validated = []
    for item in tasks:
        if not item.get("id") or not item.get("task") or not item.get("harness") or not item.get("work_dir"):
            raise ValueError(f"task queue 缺少必要欄位：{item}")
        normalized = dict(item)
        normalized["work_dir"] = validate_work_dir(item["work_dir"])
        normalized["aider_files"] = item.get("aider_files", [])
        normalized["max_iter"] = int(item.get("max_iter", 8))
        normalized["score_pattern"] = item.get("score_pattern", "")
        normalized["aider_model"] = item.get("aider_model", DEFAULT_AIDER_MODEL)
        validated.append(normalized)
    return validated


def run_command(cmd: list[str], *, cwd: Path, log_path: Path) -> subprocess.CompletedProcess:
    started = datetime.now().isoformat()
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    log_block = [
        f"[{started}] $ {' '.join(cmd)}",
        "",
        result.stdout.rstrip(),
        "",
        result.stderr.rstrip(),
        "",
        f"exit_code={result.returncode}",
        "\n" + ("-" * 80) + "\n",
    ]
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(log_block))
    return result


def export_training_snapshot(run_dir: Path, label: str) -> Path:
    exports_dir = ensure_dir(run_dir / "exports")
    output_path = exports_dir / f"{label}_training.jsonl"
    subprocess.run(
        [
            sys.executable,
            str(DREAM_CYCLE),
            "--export-training-data",
            "--include-failed",
            "--output",
            str(output_path),
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )
    return output_path


def export_status_snapshot(run_dir: Path, label: str) -> Path:
    status_path = ensure_dir(run_dir / "status") / f"{label}_status.txt"
    result = subprocess.run(
        [sys.executable, str(DREAM_CYCLE), "--status"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )
    write_text(status_path, result.stdout + ("\n" + result.stderr if result.stderr else ""))
    return status_path


def build_main_loop_cmd(task: dict, dry_run: bool) -> list[str]:
    cmd = [
        sys.executable,
        str(MAIN_LOOP),
        "--task",
        task["task"],
        "--harness",
        task["harness"],
        "--work-dir",
        task["work_dir"],
        "--max-iter",
        str(task.get("max_iter", 8)),
        "--aider-model",
        task.get("aider_model", DEFAULT_AIDER_MODEL),
    ]
    aider_files = task.get("aider_files", [])
    if aider_files:
        cmd.extend(["--aider-files", " ".join(aider_files)])
    if task.get("score_pattern"):
        cmd.extend(["--score-pattern", task["score_pattern"]])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description="連續執行系統層訓練流程")
    parser.add_argument("--duration-hours", type=float, default=6.0, help="連續執行時長，預設 6 小時")
    parser.add_argument("--task-queue", default="", help="task queue JSON")
    parser.add_argument("--suite", action="append", default=[], help="benchmark suite JSON，可重複指定")
    parser.add_argument("--export-every-min", type=int, default=30, help="每隔幾分鐘匯出 training data")
    parser.add_argument("--status-every-min", type=int, default=30, help="每隔幾分鐘輸出 status snapshot")
    parser.add_argument("--pause-sec", type=int, default=5, help="每個 cycle 間暫停秒數")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT), help="training run 輸出根目錄")
    parser.add_argument("--dry-run", action="store_true", help="透傳給 benchmark/main_loop 的 dry-run")
    parser.add_argument("--skip-morning", action="store_true", help="略過 startup.sh morning")
    parser.add_argument("--skip-evening", action="store_true", help="略過 startup.sh evening")
    args = parser.parse_args()

    if not args.task_queue and not args.suite:
        raise SystemExit("至少需要提供 --task-queue 或 --suite")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_system_training"
    run_dir = ensure_dir(Path(args.run_root).resolve() / run_id)
    log_path = run_dir / "session.log"
    events_path = run_dir / "events.jsonl"
    benchmarks_dir = ensure_dir(run_dir / "benchmark_reports")

    task_queue = load_task_queue(Path(args.task_queue).resolve()) if args.task_queue else []
    suite_paths = [Path(path).resolve() for path in args.suite]

    manifest = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "duration_hours": args.duration_hours,
        "task_queue": [str(Path(args.task_queue).resolve())] if args.task_queue else [],
        "suites": [str(path) for path in suite_paths],
        "dry_run": args.dry_run,
    }
    write_text(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    if not args.skip_morning:
        result = run_command(["bash", str(STARTUP), "morning"], cwd=BASE_DIR, log_path=log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "startup_morning",
            "exit_code": result.returncode,
        })

    deadline = time.time() + args.duration_hours * 3600
    last_export_at = 0.0
    last_status_at = 0.0
    task_index = 0
    cycle = 0

    while time.time() < deadline:
        cycle += 1
        cycle_started = datetime.now().isoformat()
        append_jsonl(events_path, {
            "time": cycle_started,
            "type": "cycle_start",
            "cycle": cycle,
        })

        for suite_path in suite_paths:
            if time.time() >= deadline:
                break
            cmd = [
                sys.executable,
                str(BENCHMARK_RUNNER),
                "--suite",
                str(suite_path),
                "--output-dir",
                str(benchmarks_dir),
            ]
            if args.dry_run:
                cmd.append("--dry-run")
            result = run_command(cmd, cwd=BASE_DIR, log_path=log_path)
            append_jsonl(events_path, {
                "time": datetime.now().isoformat(),
                "type": "benchmark_suite",
                "cycle": cycle,
                "suite": str(suite_path),
                "exit_code": result.returncode,
            })

        if task_queue and time.time() < deadline:
            task = task_queue[task_index % len(task_queue)]
            task_index += 1
            result = run_command(
                build_main_loop_cmd(task, args.dry_run),
                cwd=BASE_DIR,
                log_path=log_path,
            )
            append_jsonl(events_path, {
                "time": datetime.now().isoformat(),
                "type": "task_run",
                "cycle": cycle,
                "task_id": task["id"],
                "work_dir": task["work_dir"],
                "exit_code": result.returncode,
            })

        now = time.time()
        if now - last_export_at >= args.export_every_min * 60:
            export_path = export_training_snapshot(run_dir, f"cycle_{cycle:03d}")
            append_jsonl(events_path, {
                "time": datetime.now().isoformat(),
                "type": "training_export",
                "cycle": cycle,
                "path": str(export_path),
            })
            last_export_at = now

        if now - last_status_at >= args.status_every_min * 60:
            status_path = export_status_snapshot(run_dir, f"cycle_{cycle:03d}")
            append_jsonl(events_path, {
                "time": datetime.now().isoformat(),
                "type": "status_snapshot",
                "cycle": cycle,
                "path": str(status_path),
            })
            last_status_at = now

        if args.pause_sec > 0 and time.time() < deadline:
            time.sleep(args.pause_sec)

    export_training_snapshot(run_dir, "final")
    export_status_snapshot(run_dir, "final")

    if not args.skip_evening:
        result = run_command(["bash", str(STARTUP), "evening"], cwd=BASE_DIR, log_path=log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "startup_evening",
            "exit_code": result.returncode,
        })

    summary = {
        "run_id": run_id,
        "finished_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "cycles": cycle,
        "task_runs": task_index,
        "suites": [str(path) for path in suite_paths],
        "dry_run": args.dry_run,
    }
    write_text(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
