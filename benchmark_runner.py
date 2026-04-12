#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_runner.py

批次執行 main_loop.py benchmark suite，輸出 JSON 與 Markdown 報告。

Suite JSON 格式：
{
  "name": "my_suite",
  "cases": [
    {
      "id": "case_001",
      "task": "修正登入 API，完成後輸出 TASK_COMPLETE",
      "harness": "pytest tests/test_auth.py -q",
      "work_dir": "/path/to/repo_snapshot",
      "aider_files": ["src/auth.py", "tests/test_auth.py"],
      "max_iter": 6,
      "score_pattern": "",
      "aider_model": "ollama/qwen3.5:9b"
    }
  ]
}
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).parent
MAIN_LOOP = BASE_DIR / "main_loop.py"
DEFAULT_OUTPUT_DIR = BASE_DIR / "benchmark_reports"
BENCHMARK_HISTORY_PATH = BASE_DIR / "memory" / "benchmark_history.jsonl"
REPO_TIMELINE_PATH = BASE_DIR / "memory" / "repo_timeline.jsonl"
DEFAULT_AIDER_MODEL = os.environ.get("OLLAMA_MODEL_AIDER", "ollama/qwen3.5:9b")


def load_suite(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def complexity_score(case: dict) -> tuple[str, int]:
    """
    Analyse task complexity by counting function patterns like (1), (2), (3).
    Returns (complexity_label, timeout_sec).
    Override with case.get("timeout_sec") if explicitly set.
    """
    task = case.get("task", "")
    matches = re.findall(r'\(\d+\)', task)
    count = len(matches)
    if count <= 1:
        complexity = "low"
        timeout = 900
    elif count == 2:
        complexity = "medium"
        timeout = 1200
    else:
        complexity = "high"
        timeout = 1800
    # Explicit override
    if case.get("timeout_sec") is not None:
        timeout = int(case["timeout_sec"])
    return complexity, timeout


def tail_text(text: str, limit: int = 1200) -> str:
    compact = text.strip()
    return compact[-limit:] if len(compact) > limit else compact


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_loop_result(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("[LoopResult] "):
            try:
                return json.loads(line[len("[LoopResult] "):])
            except json.JSONDecodeError:
                return {}
    return {}


def run_case(case: dict, dry_run: bool = False) -> dict:
    started_at = datetime.now().isoformat()
    begin = time.time()

    cmd = [
        sys.executable,
        str(MAIN_LOOP),
        "--task", case["task"],
        "--harness", case["harness"],
        "--work-dir", case["work_dir"],
        "--max-iter", str(case.get("max_iter", 8)),
        "--aider-model", case.get("aider_model", DEFAULT_AIDER_MODEL),
    ]

    aider_files = case.get("aider_files", [])
    if aider_files:
        cmd.extend(["--aider-files", " ".join(aider_files)])
    if case.get("score_pattern"):
        cmd.extend(["--score-pattern", case["score_pattern"]])
    if dry_run:
        cmd.append("--dry-run")

    complexity, task_timeout = complexity_score(case)
    # benchmark 保留 recall 路徑，避免把記憶檢索一起關掉而無法評估其效果。
    bench_env = {
        **os.environ,
        "GOVERNOR_FAST_MODE": "1",
        "SKIP_MEMPALACE": "1",
        "SKIP_BRIDGE": "1",
        "SKIP_RECALL": "0",
    }
    try:
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=task_timeout,
            env=bench_env,
        )
    except subprocess.TimeoutExpired as e:
        duration = round(time.time() - begin, 2)
        stdout_tail = tail_text((e.stdout or b"").decode("utf-8", errors="replace"))
        stderr_tail = tail_text((e.stderr or b"").decode("utf-8", errors="replace"))
        return {
            "id": case["id"],
            "task": case["task"],
            "started_at": started_at,
            "duration_sec": duration,
            "passed": False,
            "exit_code": -1,
            "work_dir": case["work_dir"],
            "harness": case["harness"],
            "aider_files": case.get("aider_files", []),
            "iterations": 0,
            "rollback_count": 0,
            "task_complete": False,
            "final_state": "timeout",
            "trace_dir": "",
            "run_id": "",
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }
    duration = round(time.time() - begin, 2)
    passed = result.returncode == 0
    loop_result = parse_loop_result(result.stdout)

    return {
        "id": case["id"],
        "task": case["task"],
        "started_at": started_at,
        "duration_sec": duration,
        "passed": passed,
        "exit_code": result.returncode,
        "work_dir": case["work_dir"],
        "harness": case["harness"],
        "aider_files": aider_files,
        "iterations": int(loop_result.get("iterations", 0)),
        "rollback_count": int(loop_result.get("rollback_count", 0)),
        "task_complete": bool(loop_result.get("task_complete", False)),
        "final_state": loop_result.get("state", ""),
        "trace_dir": loop_result.get("trace_dir", ""),
        "run_id": loop_result.get("run_id", ""),
        "stdout_tail": tail_text(result.stdout),
        "stderr_tail": tail_text(result.stderr),
    }


def summarize_report(suite_name: str, cases: list[dict]) -> dict:
    total = len(cases)
    passed = sum(1 for case in cases if case["passed"])
    failed = total - passed
    total_duration = round(sum(case["duration_sec"] for case in cases), 2)
    avg_iterations = round(
        sum(case.get("iterations", 0) for case in cases) / total,
        2,
    ) if total else 0.0
    total_rollbacks = sum(case.get("rollback_count", 0) for case in cases)
    return {
        "suite": suite_name,
        "generated_at": datetime.now().isoformat(),
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round((passed / total), 4) if total else 0.0,
        "total_duration_sec": total_duration,
        "avg_iterations": avg_iterations,
        "total_rollbacks": total_rollbacks,
        "cases": cases,
    }


def write_markdown(report: dict, path: Path) -> None:
    lines = [
        f"# Benchmark Report: {report['suite']}",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- total: {report['total']}",
        f"- passed: {report['passed']}",
        f"- failed: {report['failed']}",
        f"- pass_rate: {report['pass_rate']:.2%}",
        f"- total_duration_sec: {report['total_duration_sec']}",
        f"- avg_iterations: {report['avg_iterations']}",
        f"- total_rollbacks: {report['total_rollbacks']}",
        "",
        "## Cases",
        "",
    ]

    for case in report["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        lines.extend([
            f"### {case['id']} [{status}]",
            "",
            f"- duration_sec: {case['duration_sec']}",
            f"- iterations: {case.get('iterations', 0)}",
            f"- rollback_count: {case.get('rollback_count', 0)}",
            f"- final_state: `{case.get('final_state', '')}`",
            f"- harness: `{case['harness']}`",
            f"- work_dir: `{case['work_dir']}`",
            f"- aider_files: `{', '.join(case['aider_files']) if case['aider_files'] else '(auto)'}`",
            "",
            "```text",
            case["stdout_tail"] or "(no stdout)",
            "```",
            "",
        ])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="批次執行 AI System benchmark suite")
    parser.add_argument("--suite", required=True, help="benchmark suite JSON")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="報告輸出目錄")
    parser.add_argument("--dry-run", action="store_true", help="透傳給 main_loop.py --dry-run")
    parser.add_argument("--stop-on-fail", action="store_true", help="遇到第一個失敗就停止")
    args = parser.parse_args()

    suite_path = Path(args.suite).resolve()
    suite = load_suite(suite_path)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Validate all cases first
    raw_cases = suite.get("cases", [])
    for case in raw_cases:
        if not case.get("id") or not case.get("task") or not case.get("harness") or not case.get("work_dir"):
            raise ValueError(f"benchmark case 缺少必要欄位：{case}")

    # Sort by complexity: low → medium → high (stable sort preserves original order within same level)
    complexity_order = {"low": 0, "medium": 1, "high": 2}
    sorted_cases = sorted(raw_cases, key=lambda c: complexity_order[complexity_score(c)[0]])

    cases = []
    for case in sorted_cases:
        comp, timeout = complexity_score(case)
        print(f"[Benchmark] {case['id']} complexity={comp} timeout={timeout}s")
        result = run_case(case, dry_run=args.dry_run)
        cases.append(result)
        print(
            f"[Benchmark] {result['id']} "
            f"{'PASS' if result['passed'] else 'FAIL'} "
            f"({result['duration_sec']}s)"
        )
        if args.stop_on_fail and not result["passed"]:
            break

    suite_name = suite.get("name", suite_path.stem)
    report = summarize_report(suite_name, cases)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{suite_name}_{timestamp}.json"
    md_path = output_dir / f"{suite_name}_{timestamp}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)
    append_jsonl(BENCHMARK_HISTORY_PATH, {
        "suite": report["suite"],
        "generated_at": report["generated_at"],
        "total": report["total"],
        "passed": report["passed"],
        "failed": report["failed"],
        "pass_rate": report["pass_rate"],
        "avg_iterations": report["avg_iterations"],
        "total_rollbacks": report["total_rollbacks"],
        "report_json": str(json_path),
        "report_md": str(md_path),
    })
    append_jsonl(REPO_TIMELINE_PATH, {
        "time": report["generated_at"],
        "event_type": "benchmark_suite",
        "summary": f"{report['suite']} pass_rate={report['pass_rate']:.2f} avg_iter={report['avg_iterations']:.2f}",
        "details": {
            "suite": report["suite"],
            "pass_rate": report["pass_rate"],
            "avg_iterations": report["avg_iterations"],
            "failed": report["failed"],
            "report_json": str(json_path),
        },
    })
    print(f"[Benchmark] JSON 報告：{json_path}")
    print(f"[Benchmark] Markdown 報告：{md_path}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
