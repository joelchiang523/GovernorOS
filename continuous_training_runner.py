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
from typing import Any


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
        normalized["depends_on"] = list(item.get("depends_on", []))
        normalized["priority"] = int(item.get("priority", 0))
        normalized["repeat"] = bool(item.get("repeat", True))
        normalized["enabled"] = bool(item.get("enabled", True))
        validated.append(normalized)
    return validated


def task_complexity_rank(task: dict) -> int:
    file_count = len(task.get("aider_files", []))
    max_iter = int(task.get("max_iter", 8))
    return file_count + max_iter


def select_next_task(
    tasks: list[dict],
    task_state: dict[str, dict],
) -> dict | None:
    ready = []
    for task in tasks:
        if not task.get("enabled", True):
            continue
        state = task_state.setdefault(task["id"], {"attempts": 0, "success": False, "last_exit_code": None})
        deps = task.get("depends_on", [])
        if any(not task_state.get(dep, {}).get("success", False) for dep in deps):
            continue
        if state["success"] and not task.get("repeat", True):
            continue
        ready.append(task)

    if not ready:
        return None

    ready.sort(
        key=lambda task: (
            -int(task.get("priority", 0)),
            task_complexity_rank(task),
            task_state.get(task["id"], {}).get("attempts", 0),
            task["id"],
        )
    )
    return ready[0]


def list_ready_tasks(tasks: list[dict], task_state: dict[str, dict]) -> list[dict]:
    ready = []
    for task in tasks:
        if not task.get("enabled", True):
            continue
        state = task_state.setdefault(task["id"], {"attempts": 0, "success": False, "last_exit_code": None})
        deps = task.get("depends_on", [])
        if any(not task_state.get(dep, {}).get("success", False) for dep in deps):
            continue
        if state["success"] and not task.get("repeat", True):
            continue
        ready.append(task)
    ready.sort(
        key=lambda task: (
            -int(task.get("priority", 0)),
            task_complexity_rank(task),
            task_state.get(task["id"], {}).get("attempts", 0),
            task["id"],
        )
    )
    return ready


def select_parallel_batch(tasks: list[dict], task_state: dict[str, dict], max_parallel: int) -> list[dict]:
    ready = list_ready_tasks(tasks, task_state)
    if max_parallel <= 1:
        return ready[:1]
    batch = []
    used_work_dirs: set[str] = set()
    for task in ready:
        work_dir = task["work_dir"]
        if work_dir in used_work_dirs:
            continue
        batch.append(task)
        used_work_dirs.add(work_dir)
        if len(batch) >= max_parallel:
            break
    return batch


def validate_task_graph(tasks: list[dict]) -> None:
    task_ids = {task["id"] for task in tasks}
    for task in tasks:
        missing = [dep for dep in task.get("depends_on", []) if dep not in task_ids]
        if missing:
            raise ValueError(f"task {task['id']} 依賴不存在：{missing}")

    visiting: set[str] = set()
    visited: set[str] = set()
    task_map = {task["id"]: task for task in tasks}

    def dfs(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError(f"task graph 存在循環依賴：{task_id}")
        visiting.add(task_id)
        for dep in task_map[task_id].get("depends_on", []):
            dfs(dep)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_map:
        dfs(task_id)


def build_task_graph_snapshot(tasks: list[dict], task_state: dict[str, dict]) -> dict:
    nodes = []
    edges = []
    ready_queue = []
    blocked = []
    completed = []
    for task in tasks:
        state = task_state.get(task["id"], {})
        deps = task.get("depends_on", [])
        dependency_ready = all(task_state.get(dep, {}).get("success", False) for dep in deps)
        if state.get("success", False) and not task.get("repeat", True):
            graph_state = "completed"
            completed.append(task["id"])
        elif dependency_ready:
            graph_state = "ready"
            ready_queue.append(task["id"])
        else:
            graph_state = "blocked"
            blocked.append(task["id"])
        nodes.append({
            "id": task["id"],
            "priority": int(task.get("priority", 0)),
            "depends_on": deps,
            "repeat": bool(task.get("repeat", True)),
            "enabled": bool(task.get("enabled", True)),
            "attempts": int(state.get("attempts", 0)),
            "success": bool(state.get("success", False)),
            "last_exit_code": state.get("last_exit_code"),
            "state": graph_state,
        })
        for dep in deps:
            edges.append({"from": dep, "to": task["id"]})

    return {
        "generated_at": datetime.now().isoformat(),
        "nodes": nodes,
        "edges": edges,
        "ready_queue": ready_queue,
        "blocked": blocked,
        "completed": completed,
    }


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


def start_task_process(task: dict, *, dry_run: bool) -> dict[str, Any]:
    cmd = build_main_loop_cmd(task, dry_run)
    started = datetime.now().isoformat()
    process = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "task": task,
        "cmd": cmd,
        "started_at": started,
        "process": process,
    }


def finalize_task_process(job: dict[str, Any], log_path: Path) -> subprocess.CompletedProcess:
    process = job["process"]
    stdout, stderr = process.communicate()
    result = subprocess.CompletedProcess(
        args=job["cmd"],
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    log_block = [
        f"[{job['started_at']}] $ {' '.join(job['cmd'])}",
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


def run_self_optimize(log_path: Path) -> subprocess.CompletedProcess:
    return run_command(
        [sys.executable, str(DREAM_CYCLE), "--self-optimize", "--apply"],
        cwd=BASE_DIR,
        log_path=log_path,
    )


def run_compile_truth(log_path: Path) -> subprocess.CompletedProcess:
    return run_command(
        [sys.executable, str(DREAM_CYCLE), "--compile-truth"],
        cwd=BASE_DIR,
        log_path=log_path,
    )


def run_warm_recall_cache(log_path: Path) -> subprocess.CompletedProcess:
    return run_command(
        [sys.executable, str(DREAM_CYCLE), "--warm-recall-cache"],
        cwd=BASE_DIR,
        log_path=log_path,
    )


def run_regression_gate(log_path: Path) -> subprocess.CompletedProcess:
    return run_command(
        [sys.executable, str(DREAM_CYCLE), "--regression-gate"],
        cwd=BASE_DIR,
        log_path=log_path,
    )


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
    parser.add_argument("--max-parallel", type=int, default=1, help="同一 cycle 最多平行派發幾個獨立 task")
    parser.add_argument("--stop-on-regression", action="store_true", help="若 regression gate blocked，立即停止後續 cycles")
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
    if task_queue:
        validate_task_graph(task_queue)
    task_state: dict[str, dict] = {
        task["id"]: {"attempts": 0, "success": False, "last_exit_code": None}
        for task in task_queue
    }
    suite_paths = [Path(path).resolve() for path in args.suite]

    manifest = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "duration_hours": args.duration_hours,
        "max_parallel": args.max_parallel,
        "stop_on_regression": args.stop_on_regression,
        "task_queue": [str(Path(args.task_queue).resolve())] if args.task_queue else [],
        "suites": [str(path) for path in suite_paths],
        "dry_run": args.dry_run,
    }
    write_text(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    if task_queue:
        write_text(
            run_dir / "task_graph.json",
            json.dumps(build_task_graph_snapshot(task_queue, task_state), ensure_ascii=False, indent=2),
        )

    if not args.skip_morning:
        result = run_command(["bash", str(STARTUP), "morning"], cwd=BASE_DIR, log_path=log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "startup_morning",
            "exit_code": result.returncode,
        })
        warm_result = run_warm_recall_cache(log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "warm_recall_cache",
            "cycle": 0,
            "exit_code": warm_result.returncode,
        })

    deadline = time.time() + args.duration_hours * 3600
    last_export_at = 0.0
    last_status_at = 0.0
    task_runs = 0
    cycle = 0

    while time.time() < deadline:
        cycle += 1
        cycle_started = datetime.now().isoformat()
        append_jsonl(events_path, {
            "time": cycle_started,
            "type": "cycle_start",
            "cycle": cycle,
        })
        if task_queue:
            graph_snapshot = build_task_graph_snapshot(task_queue, task_state)
            write_text(
                run_dir / "task_graph_state.json",
                json.dumps(graph_snapshot, ensure_ascii=False, indent=2),
            )
            append_jsonl(events_path, {
                "time": datetime.now().isoformat(),
                "type": "task_ready_queue",
                "cycle": cycle,
                "ready_queue": graph_snapshot["ready_queue"],
                "blocked": graph_snapshot["blocked"],
                "completed": graph_snapshot["completed"],
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
            batch = select_parallel_batch(task_queue, task_state, max(1, int(args.max_parallel)))
            if not batch:
                append_jsonl(events_path, {
                    "time": datetime.now().isoformat(),
                    "type": "task_waiting_dependencies",
                    "cycle": cycle,
                })
            else:
                append_jsonl(events_path, {
                    "time": datetime.now().isoformat(),
                    "type": "task_batch_start",
                    "cycle": cycle,
                    "task_ids": [task["id"] for task in batch],
                    "max_parallel": args.max_parallel,
                })
                jobs = []
                for task in batch:
                    task_runs += 1
                    state = task_state.setdefault(task["id"], {"attempts": 0, "success": False, "last_exit_code": None})
                    state["attempts"] += 1
                    jobs.append(start_task_process(task, dry_run=args.dry_run))

                for job in jobs:
                    task = job["task"]
                    state = task_state.setdefault(task["id"], {"attempts": 0, "success": False, "last_exit_code": None})
                    result = finalize_task_process(job, log_path)
                    state["last_exit_code"] = result.returncode
                    state["success"] = result.returncode == 0
                    append_jsonl(events_path, {
                        "time": datetime.now().isoformat(),
                        "type": "task_run",
                        "cycle": cycle,
                        "task_id": task["id"],
                        "work_dir": task["work_dir"],
                        "priority": task.get("priority", 0),
                        "depends_on": task.get("depends_on", []),
                        "attempts": state["attempts"],
                        "success": state["success"],
                        "exit_code": result.returncode,
                    })

        optimize_result = run_self_optimize(log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "self_optimize",
            "cycle": cycle,
            "exit_code": optimize_result.returncode,
        })

        truth_result = run_compile_truth(log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "compile_truth",
            "cycle": cycle,
            "exit_code": truth_result.returncode,
        })

        regression_result = run_regression_gate(log_path)
        append_jsonl(events_path, {
            "time": datetime.now().isoformat(),
            "type": "regression_gate",
            "cycle": cycle,
            "exit_code": regression_result.returncode,
        })
        if args.stop_on_regression:
            gate_path = BASE_DIR / "memory" / "regression_gate.json"
            try:
                gate = load_json(gate_path)
            except Exception:
                gate = {}
            if gate.get("blocked"):
                append_jsonl(events_path, {
                    "time": datetime.now().isoformat(),
                    "type": "regression_stop",
                    "cycle": cycle,
                    "regressions": gate.get("regressions", []),
                })
                break

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
        "task_runs": task_runs,
        "task_state": task_state,
        "task_graph": build_task_graph_snapshot(task_queue, task_state) if task_queue else {},
        "suites": [str(path) for path in suite_paths],
        "dry_run": args.dry_run,
    }
    write_text(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
