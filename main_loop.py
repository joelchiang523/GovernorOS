#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_loop.py — AI System v3.0 主控迴圈

Usage:
  python main_loop.py \\
    --task    "實作 RSI 超買超賣過濾器" \\
    --harness "pytest tests/ -q" \\
    [--aider-model claude-3-5-sonnet] \\
    [--aider-files src/signals.py src/core.py] \\
    [--work-dir /path/to/project] \\
    [--max-iter 20] \\
    [--score-pattern "score=(\\d+\\.\\d+)"] \\
    [--ollama-model prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m] \\
    [--dry-run]

狀態機：
  continue    → Aider 繼續執行
  switch_tool → 切換至 Autoresearch（Ollama 200字 brief）
  rollback    → git reset --hard HEAD^，重新嘗試
  stop        → 任務完成或無法繼續，退出迴圈
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ── aider 執行檔路徑（優先 PATH，其次 conda 環境）────────────
AIDER_BIN: str = (
    shutil.which("aider")
    or os.path.expanduser("~/.local/bin/aider")
)

# ── Aider subprocess 超時（本地大模型生成較慢）────────────────
AIDER_TIMEOUT: int = int(os.environ.get("AIDER_TIMEOUT", "900"))   # 預設 15 分鐘

BASE_DIR    = Path(__file__).parent
MEMORY_DIR  = BASE_DIR / "memory"
CLAUDE_MD   = BASE_DIR / "CLAUDE.md"
PROMPT_MD   = BASE_DIR / "PROMPT.md"
DREAM_CYCLE = BASE_DIR / "dream_cycle.py"
DIFF_INTEL  = BASE_DIR / "git_diff_intel.py"
L3_JSONL    = MEMORY_DIR / "L3_episodes.jsonl"
L4_JSON     = MEMORY_DIR / "L4_knowledge.json"
L5_JSON     = MEMORY_DIR / "L5_strategies.json"
TRACE_DIR   = MEMORY_DIR / "traces"

OLLAMA_URL = "http://localhost:11434/api/generate"

# ── 雙模型分工（與 dream_cycle.py 保持一致）──────────────
# Governor：狀態機判斷 / Harness 解讀 / MemPalace 治理
OLLAMA_MODEL_GOVERNOR   = os.environ.get(
    "OLLAMA_MODEL_GOVERNOR", "qwen3.5:27b"
)
# Researcher：Autoresearch 200字 brief（高服從、快速產出）
OLLAMA_MODEL_RESEARCHER = os.environ.get(
    "OLLAMA_MODEL_RESEARCHER",
    "qwen2.5:7b",
)
OLLAMA_MODEL = OLLAMA_MODEL_GOVERNOR  # 向後相容

WAKE_L4_MIN_CONF = 0.70
WAKE_L5_MIN_CONF = 0.75


# ─────────────────────────────────────────────
# 狀態機
# ─────────────────────────────────────────────

class LoopStateMachine:
    """
    轉換規則：
      harness pass                  → continue（重置連敗計數）
      consecutive_fails == 1        → continue（讓 Aider 自己修）
      consecutive_fails == 2        → switch_tool（Autoresearch 找方向）
      consecutive_fails >= 3        → rollback
      rollback_count >= 2 且仍 fail → stop（升級給人工）
      aider 回傳 TASK_COMPLETE      → stop
      達到 max_iter                 → stop
    """

    def __init__(self):
        self.consecutive_fails = 0
        self.rollback_count    = 0
        self.state             = "continue"

    def update(self, harness_pass: bool, task_complete: bool = False) -> str:
        # task_complete 只有在 harness 也通過時才真正停止
        # 若模型宣告完成但 harness 仍失敗，繼續迭代
        if task_complete and harness_pass:
            self.state = "stop"
            return self.state

        if harness_pass:
            self.consecutive_fails = 0
            self.state = "continue"
        else:
            self.consecutive_fails += 1
            if self.consecutive_fails >= 3:
                if self.rollback_count >= 2:
                    print("[StateMachine] 連敗 + 多次 rollback，升級給人工介入")
                    self.state = "stop"
                else:
                    self.state = "rollback"
            elif self.consecutive_fails == 2:
                self.state = "switch_tool"
            else:
                self.state = "continue"

        return self.state

    def on_rollback_done(self):
        self.rollback_count    += 1
        self.consecutive_fails  = 0
        self.state              = "continue"

    def on_research_done(self):
        self.state = "continue"

    def summary(self) -> str:
        return (f"state={self.state} "
                f"consecutive_fails={self.consecutive_fails} "
                f"rollback_count={self.rollback_count}")


def load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8").strip()
        return json.loads(content) if content else []
    except json.JSONDecodeError:
        print(f"[WARN] JSON 解析失敗：{path}")
        return []


def load_jsonl_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def normalize_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_./-]+", text)
        if len(token) >= 2
    }


def memory_relevance_score(item: dict, task_tokens: set[str], file_tokens: set[str]) -> float:
    fields = [
        str(item.get("id", "")),
        str(item.get("pattern", "")),
        str(item.get("condition", "")),
        str(item.get("action", "")),
        str(item.get("avoid", "")),
        str(item.get("scope", "")),
        str(item.get("source", "")),
        str(item.get("combo", "")),
    ]
    haystack = " ".join(fields)
    item_tokens = normalize_tokens(haystack)
    if not item_tokens:
        return float(item.get("confidence", 0.0))

    task_overlap = len(task_tokens & item_tokens)
    file_overlap = len(file_tokens & item_tokens)
    return (
        float(item.get("confidence", 0.0))
        + task_overlap * 1.5
        + file_overlap * 2.0
    )


def select_relevant_memories(task: str, aider_files: list[str]) -> tuple[list[dict], list[dict]]:
    task_tokens = normalize_tokens(task)
    file_tokens = set()
    for file_path in aider_files:
        file_tokens |= normalize_tokens(file_path)
        file_tokens |= normalize_tokens(Path(file_path).name)

    l4_all = [
        item for item in load_json_list(L4_JSON)
        if item.get("status") == "active"
        and float(item.get("confidence", 0.0)) >= WAKE_L4_MIN_CONF
    ]
    l5_all = [
        item for item in load_json_list(L5_JSON)
        if item.get("status") == "active"
        and float(item.get("confidence", 0.0)) >= WAKE_L5_MIN_CONF
        and item.get("conflict_check") != "needs_review"
    ]

    l4_sorted = sorted(
        l4_all,
        key=lambda item: memory_relevance_score(item, task_tokens, file_tokens),
        reverse=True,
    )
    l5_sorted = sorted(
        l5_all,
        key=lambda item: memory_relevance_score(item, task_tokens, file_tokens),
        reverse=True,
    )
    return l4_sorted[:3], l5_sorted[:3]


def format_memory_section(title: str, items: list[dict], formatter) -> str:
    if not items:
        return ""
    lines = [title]
    for item in items:
        lines.append(formatter(item))
    return "\n".join(lines)


def sanitize_cli_text(text: str, limit: int = 1200) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def slugify(value: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug[:max_len] or "task"


def infer_task_type(task: str, harness_cmd: str, aider_files: list[str]) -> str:
    blob = " ".join([task, harness_cmd, " ".join(aider_files)]).lower()
    if any(token in blob for token in ["fix", "bug", "修正", "錯誤", "失敗", "exception"]):
        return "bugfix"
    if any(token in blob for token in ["refactor", "cleanup", "rename", "重構", "整理"]):
        return "refactor"
    if any(token in blob for token in ["test", "pytest", "unittest", "測試"]):
        return "test_task"
    if any(token in blob for token in ["config", "yaml", "json", "toml", "設定"]):
        return "config"
    if any(token in blob for token in ["investigate", "research", "analyze", "debug", "調查", "分析"]):
        return "investigation"
    if any(token in blob for token in ["add", "create", "implement", "新增", "建立", "實作"]):
        return "feature"
    return "general"


def parse_task_spec(task: str, harness_cmd: str, aider_files: list[str]) -> dict:
    lines = [line.strip(" -\t") for line in re.split(r"[\n\r]+", task) if line.strip()]
    normalized = " ".join(lines) if lines else task.strip()
    clauses = [part.strip() for part in re.split(r"[。；;]", normalized) if part.strip()]

    goal = clauses[0] if clauses else normalized
    constraints = []
    acceptance = []
    for clause in clauses[1:]:
        lower = clause.lower()
        if any(token in lower for token in ["不要", "限制", "only", "avoid", "不得", "只修改", "不可"]):
            constraints.append(clause)
        elif any(token in lower for token in ["必須", "需", "驗收", "測試", "pass", "通過", "完成後"]):
            acceptance.append(clause)

    if not acceptance and harness_cmd:
        acceptance.append(f"通過 harness：{harness_cmd}")
    if not constraints and aider_files:
        constraints.append(f"優先處理檔案：{', '.join(aider_files[:8])}")

    return {
        "goal": goal[:240],
        "constraints": constraints[:6],
        "acceptance": acceptance[:6],
    }


def infer_task_complexity(task: str, aider_files: list[str], max_iter: int) -> str:
    score = 0
    lower_task = task.lower()
    if len(aider_files) >= 4:
        score += 2
    elif len(aider_files) >= 2:
        score += 1
    if max_iter >= 10:
        score += 1
    if any(token in lower_task for token in ["cross", "multiple", "architecture", "system", "跨", "多個", "整體"]):
        score += 2
    if any(token in lower_task for token in ["refactor", "migrate", "重構", "遷移"]):
        score += 1
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def collect_file_history(task_type: str, limit: int = 200) -> dict[str, dict]:
    episodes = load_jsonl_records(L3_JSONL)[-limit:]
    stats: dict[str, dict] = {}
    for ep in episodes:
        files = ep.get("context", {}).get("files", [])
        if not files:
            continue
        ep_task_type = ep.get("workflow", {}).get("task_type", "")
        weight = 1.5 if ep_task_type and ep_task_type == task_type else 1.0
        success = int(ep.get("status", 1) == 0)
        for file_path in files:
            bucket = stats.setdefault(file_path, {
                "uses": 0,
                "successes": 0,
                "fails": 0,
                "task_type_matches": 0,
                "score": 0.0,
            })
            bucket["uses"] += 1
            if success:
                bucket["successes"] += 1
                bucket["score"] += 1.5 * weight
            else:
                bucket["fails"] += 1
                bucket["score"] -= 1.0 * weight
            if ep_task_type and ep_task_type == task_type:
                bucket["task_type_matches"] += 1
    return stats


def build_file_plan(task: str, aider_files: list[str], task_type: str) -> list[dict]:
    task_tokens = normalize_tokens(task)
    history = collect_file_history(task_type)
    plans = []
    for file_path in aider_files:
        score = 0.0
        reasons: list[str] = []
        file_tokens = normalize_tokens(file_path) | normalize_tokens(Path(file_path).name)
        overlap = sorted(task_tokens & file_tokens)
        if overlap:
            score += len(overlap) * 2.0
            reasons.append(f"task_overlap={','.join(overlap[:4])}")
        if "/test" in file_path.lower() or "test_" in Path(file_path).name.lower():
            score += 1.0
            reasons.append("test_file")
        if Path(file_path).suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".gd", ".cs"}:
            score += 0.5
            reasons.append("source_file")
        hist = history.get(file_path)
        if hist:
            score += hist.get("score", 0.0)
            reasons.append(f"history={hist.get('successes', 0)}p/{hist.get('fails', 0)}f")
            if hist.get("task_type_matches", 0) > 0:
                score += 0.5
                reasons.append("task_type_match")
        plans.append({
            "file": file_path,
            "score": round(score, 2),
            "reasons": reasons or ["provided_by_user"],
        })
    return sorted(plans, key=lambda item: item["score"], reverse=True)


def summarize_file_plan(file_plan: list[dict], limit: int = 5) -> str:
    if not file_plan:
        return ""
    parts = []
    for item in file_plan[:limit]:
        reasons = "/".join(item.get("reasons", [])[:3])
        parts.append(f"{item['file']}@{item['score']:.1f}<{reasons}>")
    return "; ".join(parts)


def build_workflow_plan(
    task: str,
    harness_cmd: str,
    aider_files: list[str],
    max_iter: int,
    consecutive_fails: int,
    research_brief: str,
    last_harness_output: str,
    selected_l4: list[dict],
    selected_l5: list[dict],
) -> dict:
    task_type = infer_task_type(task, harness_cmd, aider_files)
    complexity = infer_task_complexity(task, aider_files, max_iter)
    parsed_task = parse_task_spec(task, harness_cmd, aider_files)
    file_plan = build_file_plan(task, aider_files, task_type)
    if consecutive_fails >= 2:
        execution_mode = "research_first"
        research_reason = "two_consecutive_failures"
    elif task_type in {"investigation", "refactor"} or complexity == "high":
        execution_mode = "cautious_patch"
        research_reason = "high_complexity_or_refactor"
    else:
        execution_mode = "direct_patch"
        research_reason = "not_required_yet"

    if consecutive_fails >= 2:
        rollback_risk = "high"
    elif consecutive_fails == 1 or complexity == "high":
        rollback_risk = "medium"
    else:
        rollback_risk = "low"

    strategy_parts = []
    if selected_l4:
        strategy_parts.append(f"L4={','.join(str(item.get('id', '')) for item in selected_l4[:3])}")
    if selected_l5:
        strategy_parts.append(f"L5={','.join(str(item.get('id', '')) for item in selected_l5[:3])}")
    if last_harness_output:
        harness_hint = sanitize_cli_text(last_harness_output[-240:], 240)
        strategy_parts.append(f"last_failure={harness_hint}")
    if research_brief:
        strategy_parts.append(f"research={sanitize_cli_text(research_brief, 160)}")

    return {
        "task_type": task_type,
        "complexity": complexity,
        "execution_mode": execution_mode,
        "research_reason": research_reason,
        "rollback_risk": rollback_risk,
        "file_plan": file_plan,
        "parsed_task": parsed_task,
        "strategy_note": " | ".join(strategy_parts)[:600],
    }


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_trace_artifacts(
    run_trace_dir: Path,
    *,
    iteration: int,
    event_type: str,
    payload: dict,
    prompt_text: str = "",
    aider_output: str = "",
    harness_output: str = "",
    diff_text: str = "",
) -> None:
    iter_dir = run_trace_dir / f"iter_{iteration:02d}_{event_type}"
    write_json_file(iter_dir / "meta.json", payload)
    if prompt_text:
        write_text_file(iter_dir / "prompt.txt", prompt_text)
    if aider_output:
        write_text_file(iter_dir / "aider_output.txt", aider_output)
    if harness_output:
        write_text_file(iter_dir / "harness_output.txt", harness_output)
    if diff_text:
        write_text_file(iter_dir / "diff.patch", diff_text)


# ─────────────────────────────────────────────
# Harness 執行
# ─────────────────────────────────────────────

def run_harness(
    cmd: str,
    work_dir: Path,
    score_pattern: Optional[str] = None,
) -> tuple[bool, float, str]:
    """
    回傳 (pass, score_delta, output)
    pass      = exit_code == 0
    score     = 從 output 中用 score_pattern 提取；無則回 0.0
    """
    result = subprocess.run(
        cmd, shell=True, cwd=work_dir,
        capture_output=True, text=True, timeout=300,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0

    score = 0.0
    if score_pattern and passed:
        m = re.search(score_pattern, output)
        if m:
            try:
                score = float(m.group(1))
            except (ValueError, IndexError):
                pass

    status_str = "PASS ✓" if passed else f"FAIL ✗ (exit={result.returncode})"
    print(f"[Harness] {status_str}")
    if not passed and output.strip():
        # 只印最後 20 行，防止 context 爆炸
        lines = output.strip().splitlines()
        print("\n".join(lines[-20:]))

    return passed, score, output


# ─────────────────────────────────────────────
# Aider 執行
# ─────────────────────────────────────────────

def run_aider(
    task_with_context: str,
    files: list[str],
    work_dir: Path,
    model: str,
    dry_run: bool = False,
) -> tuple[str, bool]:
    """
    呼叫 Aider CLI，回傳 (output, task_complete)
    task_complete = output 包含 TASK_COMPLETE
    """
    if dry_run:
        print("[Aider] DRY RUN，略過實際執行")
        return "[DRY RUN]", False

    file_args = " ".join(f'"{f}"' for f in files) if files else ""
    message   = task_with_context.replace('"', '\\"')

    cmd = (
        f'"{AIDER_BIN}" --yes-always --no-auto-commits '
        f'--no-detect-urls --no-auto-lint --map-tokens 0 '
        f'--no-browser --no-show-model-warnings '
        f'--model {model} '
        f'{file_args} '
        f'--message "{message}"'
    )

    print(f"[Aider] 執行指令：{cmd[:140]}...")
    print(f"[Aider] aider 路徑：{AIDER_BIN}  超時：{AIDER_TIMEOUT}s")
    result = subprocess.run(
        cmd, shell=True, cwd=work_dir,
        capture_output=True, text=True, timeout=AIDER_TIMEOUT,
    )
    output = result.stdout + result.stderr

    if result.returncode != 0:
        print(f"[Aider] 執行異常（exit={result.returncode}）")
        lines = output.strip().splitlines()
        print("\n".join(lines[-10:]))

    # 只有當 TASK_COMPLETE 獨立成行時才視為完成（避免把任務描述中的字串誤判）
    task_complete = bool(re.search(r'^\s*TASK_COMPLETE\s*$', output, re.MULTILINE))
    return output, task_complete


# ─────────────────────────────────────────────
# Git 工具
# ─────────────────────────────────────────────

def get_git_diff(work_dir: Path) -> tuple[str, list[str], int]:
    """回傳 (diff_text, changed_files, lines_changed)"""
    result = subprocess.run(
        "git diff HEAD --stat",
        shell=True, cwd=work_dir, capture_output=True, text=True,
    )
    stat = result.stdout.strip()

    diff_result = subprocess.run(
        "git diff HEAD",
        shell=True, cwd=work_dir, capture_output=True, text=True,
    )
    diff_text = diff_result.stdout[:4000]   # 截斷，避免 context 過大

    # 解析 changed files
    files = re.findall(r"^\s+(\S+)\s+\|", stat, re.MULTILINE)

    # 解析 lines_changed
    m = re.search(r"(\d+) insertion", stat)
    lines = int(m.group(1)) if m else 0
    m2 = re.search(r"(\d+) deletion", stat)
    lines += int(m2.group(1)) if m2 else 0

    return diff_text, files, lines


def git_snapshot(work_dir: Path, iteration: int) -> str:
    """
    在 Harness 執行前建立 snapshot：
      1. git add -A + commit（--allow-empty 保證 tag 有落點）
      2. 建立輕量 tag iter_{N}_pre_harness（-f 覆蓋同名舊 tag）
    回傳 tag 名稱。
    """
    tag = f"iter_{iteration}_pre_harness"
    subprocess.run("git add -A", shell=True, cwd=work_dir, capture_output=True)
    subprocess.run(
        f'git commit -m "[snapshot] {tag}" --allow-empty',
        shell=True, cwd=work_dir, capture_output=True,
    )
    subprocess.run(
        f"git tag -f {tag}",
        shell=True, cwd=work_dir, capture_output=True,
    )
    print(f"[Git] Snapshot 建立：{tag}")
    return tag


def git_rollback(work_dir: Path, target_tag: Optional[str] = None) -> bool:
    """
    回滾到指定 snapshot tag。
    - target_tag 有值：git reset --hard <tag>（精確回到任意一輪）
    - target_tag 為 None：fallback 到 HEAD^
    """
    if target_tag:
        result = subprocess.run(
            f"git reset --hard {target_tag}",
            shell=True, cwd=work_dir, capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"[Git] Rollback → snapshot {target_tag} 成功")
            return True
        print(f"[Git] Rollback to {target_tag} 失敗：{result.stderr.strip()}")

    # fallback
    result = subprocess.run(
        "git reset --hard HEAD^",
        shell=True, cwd=work_dir, capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("[Git] Rollback → HEAD^ 成功（fallback）")
        return True

    subprocess.run("git restore .", shell=True, cwd=work_dir, capture_output=True)
    print("[Git] Rollback → git restore . 已執行（最終 fallback）")
    return True


def git_add_commit(work_dir: Path, message: str) -> bool:
    subprocess.run("git add -A", shell=True, cwd=work_dir)
    result = subprocess.run(
        f'git commit -m "{message}"',
        shell=True, cwd=work_dir, capture_output=True, text=True,
    )
    return result.returncode == 0


# ─────────────────────────────────────────────
# Autoresearch（Ollama 200字 brief）
# ─────────────────────────────────────────────

def autoresearch(task: str, harness_output: str, context: str) -> str:
    """
    呼叫 Ollama Researcher（Gemma），回傳不超過 200 字的研究 brief。
    使用 Researcher 而非 Governor：輸出短、結構固定、邊界明確，
    不直接影響 rollback/stop 決策，適合高服從模型快速產出。
    """
    prompt = f"""你是程式除錯研究員。根據以下資訊，給出 200 字以內的具體研究結論。

任務：{task}

Harness 失敗輸出（最後部分）：
{harness_output[-800:]}

當前 context：
{context[:400]}

要求：
- 具體說明失敗的根本原因（不是現象，是原因）
- 給出 1-2 個具體的修改方向
- 嚴格不超過 200 字
- 使用繁體中文
- 禁止包含程式碼片段
"""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL_RESEARCHER, "prompt": prompt, "stream": False},
            timeout=300,
        )
        resp.raise_for_status()
        brief = resp.json().get("response", "").strip()
        # 確保不超過 200 字
        if len(brief) > 400:
            brief = brief[:400] + "…（已截斷）"
        return brief
    except Exception as e:
        print(f"[Research] Ollama 呼叫失敗：{e}")
        return ""


# ─────────────────────────────────────────────
# dream_cycle.py 橋接
# ─────────────────────────────────────────────

def dc(args: list[str]) -> None:
    """呼叫 dream_cycle.py 的快捷函式"""
    subprocess.run(
        [sys.executable, str(DREAM_CYCLE)] + args,
        cwd=BASE_DIR,
    )


def record_episode(
    task: str,
    harness_pass: bool,
    diff_summary: str,
    delta: float,
    *,
    files: list[str],
    iteration: int,
    max_iter: int,
    aider_model: str,
    harness_cmd: str,
    work_dir: Path,
    prompt_excerpt: str,
    harness_excerpt: str,
    selected_l4_ids: list[str],
    selected_l5_ids: list[str],
    patch_summary: str,
    task_complete: bool,
    run_id: str,
    trace_dir: Path,
    failure_mode: str,
    root_cause: str,
    patch_type: str,
    repo_type: str,
    language: str,
    framework: str,
    test_scope: str,
    workflow_task_type: str,
    workflow_complexity: str,
    workflow_mode: str,
    workflow_file_plan: str,
    workflow_goal: str,
    workflow_constraints: str,
    workflow_acceptance: str,
    workflow_research_reason: str,
    workflow_rollback_reason: str,
    workflow_strategy_note: str,
):
    status_code = 0 if harness_pass else 1
    args = [
        "--record",
        f"--task={task[:200]}",
        f"--status-code={status_code}",
        f"--diff-summary={diff_summary[:300]}",
        f"--harness-delta={delta:.2f}",
        f"--files={','.join(files[:20])}",
        f"--iteration={iteration}",
        f"--max-iter={max_iter}",
        f"--aider-model={aider_model[:120]}",
        f"--harness-cmd={sanitize_cli_text(harness_cmd, 300)}",
        f"--work-dir={str(work_dir)[:300]}",
        f"--prompt-excerpt={sanitize_cli_text(prompt_excerpt, 1500)}",
        f"--harness-excerpt={sanitize_cli_text(harness_excerpt, 1200)}",
        f"--selected-l4-ids={','.join(selected_l4_ids[:10])}",
        f"--selected-l5-ids={','.join(selected_l5_ids[:10])}",
        f"--patch-summary={sanitize_cli_text(patch_summary, 1200)}",
        f"--task-complete={1 if task_complete else 0}",
        f"--run-id={run_id}",
        f"--trace-dir={str(trace_dir)[:400]}",
        f"--failure-mode={failure_mode[:120]}",
        f"--root-cause={root_cause[:160]}",
        f"--patch-type={patch_type[:80]}",
        f"--repo-type={repo_type[:80]}",
        f"--language={language[:80]}",
        f"--framework={framework[:80]}",
        f"--test-scope={test_scope[:120]}",
        f"--workflow-task-type={workflow_task_type[:80]}",
        f"--workflow-complexity={workflow_complexity[:80]}",
        f"--workflow-mode={workflow_mode[:80]}",
        f"--workflow-file-plan={sanitize_cli_text(workflow_file_plan, 1200)}",
        f"--workflow-goal={sanitize_cli_text(workflow_goal, 400)}",
        f"--workflow-constraints={sanitize_cli_text(workflow_constraints, 800)}",
        f"--workflow-acceptance={sanitize_cli_text(workflow_acceptance, 800)}",
        f"--workflow-research-reason={workflow_research_reason[:160]}",
        f"--workflow-rollback-reason={workflow_rollback_reason[:160]}",
        f"--workflow-strategy-note={sanitize_cli_text(workflow_strategy_note, 1200)}",
    ]
    dc(args)


def update_mempalace(context: str):
    dc(["--update-mempalace", f"--context={context[:2000]}"])


def bridge_to_l3(context: str):
    dc(["--bridge", f"--context={context[:2000]}"])


def record_diff_intel(
    patch_type: str,
    files: list[str],
    harness_result: str,
    delta: float,
    lines: int,
    rollback: bool,
):
    subprocess.run(
        [sys.executable, str(DIFF_INTEL),
         "--record",
         f"--patch-type={patch_type}",
         f"--files={','.join(files)}",
         f"--harness={harness_result}",
         f"--delta={delta:.2f}",
         f"--lines={lines}",
         f"--rollback={1 if rollback else 0}"],
        cwd=BASE_DIR,
    )


# ─────────────────────────────────────────────
# PROMPT.md context 更新
# ─────────────────────────────────────────────

def update_prompt_context(task: str, iteration: int, max_iter: int,
                           harness_pass: bool, state: str,
                           consecutive_fails: int, research_brief: str = ""):
    path = PROMPT_MD
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")

    # 更新 GOAL_INJECT
    goal_block = (
        f"**任務**：{task}\n\n"
        f"**狀態**：[ITER {iteration}/{max_iter}]  "
        f"[HARNESS: {'PASS' if harness_pass else 'FAIL'}]  "
        f"[STATE: {state}]  "
        f"[CONSECUTIVE_FAILS: {consecutive_fails}]"
    )
    if research_brief:
        goal_block += f"\n\n**Research Brief**：\n{research_brief}"

    pattern = re.compile(
        r"<!-- GOAL_INJECT_START -->.*?<!-- GOAL_INJECT_END -->",
        re.DOTALL,
    )
    replacement = f"<!-- GOAL_INJECT_START -->\n{goal_block}\n<!-- GOAL_INJECT_END -->"
    updated = pattern.sub(replacement, content) if pattern.search(content) else (
        content + f"\n<!-- GOAL_INJECT_START -->\n{goal_block}\n<!-- GOAL_INJECT_END -->\n"
    )
    path.write_text(updated, encoding="utf-8")


# ─────────────────────────────────────────────
# 主迴圈
# ─────────────────────────────────────────────

def main_loop(
    task: str,
    harness_cmd: str,
    aider_model: str,
    aider_files: list[str],
    work_dir: Path,
    max_iter: int,
    score_pattern: Optional[str],
    dry_run: bool,
):
    sm         = LoopStateMachine()
    baseline_score = 0.0
    prev_score     = 0.0
    last_harness_output = ""
    research_brief      = ""
    log_lines: list[str] = []
    last_good_snapshot: Optional[str] = None   # 最後一次 harness PASS 前的 snapshot tag
    final_harness_pass = False
    final_task_complete = False
    iteration = 0
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slugify(task, 32)}"
    run_trace_dir = TRACE_DIR / run_id
    run_trace_dir.mkdir(parents=True, exist_ok=True)
    write_json_file(run_trace_dir / "run_meta.json", {
        "run_id": run_id,
        "task": task,
        "harness_cmd": harness_cmd,
        "aider_model": aider_model,
        "aider_files": aider_files,
        "work_dir": str(work_dir),
        "max_iter": max_iter,
        "started_at": datetime.now().isoformat(),
    })

    print(f"\n{'═'*60}")
    print(f"  AI System v3.0 主控迴圈啟動")
    print(f"  任務：{task}")
    print(f"  Harness：{harness_cmd}")
    print(f"  最大迭代：{max_iter}")
    print(f"{'═'*60}\n")

    # 取得 Harness baseline
    print("[Loop] 執行 Harness baseline...")
    base_pass, baseline_score, baseline_output = run_harness(harness_cmd, work_dir, score_pattern)
    prev_score = baseline_score
    final_harness_pass = base_pass
    print(f"[Loop] Baseline score={baseline_score:.2f}  pass={base_pass}")
    save_trace_artifacts(
        run_trace_dir,
        iteration=0,
        event_type="baseline",
        payload={
            "run_id": run_id,
            "event_type": "baseline",
            "baseline_pass": base_pass,
            "baseline_score": baseline_score,
            "state": sm.state,
        },
        harness_output=baseline_output,
    )

    for iteration in range(1, max_iter + 1):
        print(f"\n{'─'*40}")
        print(f"[Loop] 迭代 {iteration}/{max_iter}  {sm.summary()}")
        print(f"{'─'*40}")

        # ── 更新 PROMPT.md context ─────────────────
        update_prompt_context(
            task, iteration, max_iter,
            harness_pass=(sm.consecutive_fails == 0),
            state=sm.state,
            consecutive_fails=sm.consecutive_fails,
            research_brief=research_brief,
        )

        # ── Rollback ───────────────────────────────
        if sm.state == "rollback":
            print(f"[Loop] 觸發 Rollback（連敗 {sm.consecutive_fails} 次）")
            # 精確回到最後一次 pass 前的 snapshot；無則 fallback
            rollback_target = last_good_snapshot or "HEAD^"
            git_rollback(work_dir, target_tag=last_good_snapshot)
            save_trace_artifacts(
                run_trace_dir,
                iteration=iteration,
                event_type="rollback",
                payload={
                    "run_id": run_id,
                    "event_type": "rollback",
                    "state_before": sm.state,
                    "rollback_target": rollback_target,
                    "consecutive_fails": sm.consecutive_fails,
                    "rollback_count": sm.rollback_count,
                },
            )
            sm.on_rollback_done()
            research_brief = ""
            log_lines.append(
                f"iter={iteration} ROLLBACK → {last_good_snapshot or 'HEAD^'}"
            )
            continue

        # ── Autoresearch ──────────────────────────
        if sm.state == "switch_tool":
            print(f"[Loop] 切換至 Autoresearch（連敗 {sm.consecutive_fails} 次）")
            if dry_run:
                print("[Loop] DRY RUN — 略過 Autoresearch")
                sm.on_research_done()
                continue
            # 讀取 L2（唯一真相），不讀 CLAUDE.md 展示層
            l2_path = BASE_DIR / "memory" / "L2_working.md"
            context_snapshot = (
                l2_path.read_text(encoding="utf-8")[:600]
                if l2_path.exists() else ""
            )
            research_brief = autoresearch(task, last_harness_output, context_snapshot)
            if research_brief:
                print(f"[Research] Brief（{len(research_brief)}字）：\n{research_brief}\n")
            save_trace_artifacts(
                run_trace_dir,
                iteration=iteration,
                event_type="research",
                payload={
                    "run_id": run_id,
                    "event_type": "research",
                    "state_before": sm.state,
                    "research_brief": research_brief,
                    "consecutive_fails": sm.consecutive_fails,
                },
                harness_output=last_harness_output,
            )
            sm.on_research_done()
            log_lines.append(f"iter={iteration} RESEARCH")
            continue

        # ── 組合給 Aider 的完整指令（讀 L2 唯一真相）──
        l2_path   = BASE_DIR / "memory" / "L2_working.md"
        mempalace = (
            l2_path.read_text(encoding="utf-8")[:800]
            if l2_path.exists() else ""
        )
        selected_l4, selected_l5 = select_relevant_memories(task, aider_files)
        workflow_plan = build_workflow_plan(
            task,
            harness_cmd,
            aider_files,
            max_iter,
            sm.consecutive_fails,
            research_brief,
            last_harness_output,
            selected_l4,
            selected_l5,
        )
        l4_section = format_memory_section(
            "[Relevant L4]",
            selected_l4,
            lambda item: (
                f"- [{item.get('id', 'L4')} conf={float(item.get('confidence', 0.0)):.2f}] "
                f"{item.get('pattern', '')} ({item.get('scope', 'general')})"
            ),
        )
        l5_section = format_memory_section(
            "[Relevant L5]",
            selected_l5,
            lambda item: (
                f"- [{item.get('id', 'L5')} conf={float(item.get('confidence', 0.0)):.2f}] "
                f"當 {item.get('condition', '')}：{item.get('action', '')} "
                f"（避免：{item.get('avoid', '')}）"
            ),
        )
        aider_message = (
            f"[ITER {iteration}/{max_iter}] 任務：{task}\n"
            + (
                f"\n[Workflow Plan]\n"
                f"- task_type={workflow_plan['task_type']}\n"
                f"- complexity={workflow_plan['complexity']}\n"
                f"- mode={workflow_plan['execution_mode']}\n"
                f"- goal={workflow_plan['parsed_task']['goal']}\n"
                f"- constraints={'; '.join(workflow_plan['parsed_task']['constraints']) or '(none)'}\n"
                f"- acceptance={'; '.join(workflow_plan['parsed_task']['acceptance']) or '(none)'}\n"
                f"- file_plan={summarize_file_plan(workflow_plan['file_plan']) or '(none)'}\n"
                if workflow_plan else ""
            )
            + (f"\n[Research Brief]\n{research_brief}\n" if research_brief else "")
            + (f"\n[MemPalace]\n{mempalace}\n" if mempalace else "")
            + (f"\n{l4_section}\n" if l4_section else "")
            + (f"\n{l5_section}\n" if l5_section else "")
            + (f"\n[上次 Harness 失敗]\n{last_harness_output[-600:]}\n"
               if last_harness_output and sm.consecutive_fails > 0 else "")
        )

        # ── Aider 執行 ────────────────────────────
        aider_out, task_complete = run_aider(
            aider_message, aider_files, work_dir, aider_model, dry_run
        )

        # ── Git Diff ──────────────────────────────
        diff_text, changed_files, lines_changed = get_git_diff(work_dir)
        diff_summary = f"files={','.join(changed_files[:5])} lines={lines_changed}"
        diff_excerpt = " | ".join(
            line.strip()
            for line in diff_text.splitlines()
            if line.strip()
        )[:600]
        patch_summary = f"{diff_summary}; diff_excerpt={diff_excerpt or '(empty diff)'}"
        candidate_files = changed_files or aider_files

        # ── Snapshot（Harness 前建立，rollback 精確點）─
        snapshot_tag = git_snapshot(work_dir, iteration)

        # ── Harness 驗證 ──────────────────────────
        harness_pass, current_score, last_harness_output = run_harness(
            harness_cmd, work_dir, score_pattern
        )
        final_harness_pass = harness_pass
        harness_delta  = current_score - prev_score
        harness_result = "pass" if harness_pass else "fail"
        if current_score > 0:
            prev_score = current_score

        harness_lower = last_harness_output.lower()
        prompt_lower = aider_message.lower()
        files_for_taxonomy = [str(path) for path in candidate_files]
        file_blob = " ".join(files_for_taxonomy).lower()
        if harness_pass:
            failure_mode = "success"
            root_cause = "validated_pass"
        elif lines_changed == 0:
            failure_mode = "no_effect_change"
            root_cause = "model_output_did_not_change_repo"
        elif any(token in harness_lower for token in ["syntaxerror", "indentationerror", "parseerror"]):
            failure_mode = "runtime_failure"
            root_cause = "syntax_error"
        elif any(token in harness_lower for token in ["importerror", "modulenotfounderror", "no module named"]):
            failure_mode = "tooling_failure"
            root_cause = "import_or_dependency_error"
        elif any(token in harness_lower for token in ["assertionerror", "assert ", "expected", " != ", " == "]):
            failure_mode = "test_failure"
            root_cause = "assertion_failure"
        elif "timeout" in harness_lower:
            failure_mode = "runtime_failure"
            root_cause = "timeout"
        elif any(token in harness_lower for token in ["attributeerror", "typeerror", "keyerror", "valueerror", "indexerror", "nameerror"]):
            failure_mode = "runtime_failure"
            root_cause = "runtime_exception"
        elif any(token in harness_lower for token in ["not found", "no such file", "filenotfounderror"]):
            failure_mode = "tooling_failure"
            root_cause = "missing_file"
        else:
            failure_mode = "test_failure"
            root_cause = "unknown_failure"

        if "pygame" in prompt_lower or "pygame" in file_blob:
            framework = "pygame"
            repo_type = "game"
        elif ".gd" in file_blob or "project.godot" in file_blob or "godot" in prompt_lower:
            framework = "godot"
            repo_type = "game"
        elif ".unity" in file_blob or "assets/" in file_blob or "projectsettings/" in file_blob or "unity" in prompt_lower:
            framework = "unity"
            repo_type = "game"
        elif "flask" in prompt_lower or "django" in prompt_lower:
            framework = "python_web"
            repo_type = "general"
        elif "react" in prompt_lower or ".tsx" in file_blob or ".jsx" in file_blob:
            framework = "react"
            repo_type = "general"
        else:
            framework = "generic"
            repo_type = "general"

        suffixes = {Path(path).suffix.lower() for path in files_for_taxonomy if Path(path).suffix}
        if ".py" in suffixes:
            language = "python"
        elif ".js" in suffixes:
            language = "javascript"
        elif ".ts" in suffixes or ".tsx" in suffixes:
            language = "typescript"
        elif ".gd" in suffixes:
            language = "gdscript"
        elif ".cs" in suffixes:
            language = "csharp"
        else:
            language = "unknown"

        harness_cmd_lower = harness_cmd.lower()
        if "pytest" in harness_cmd_lower:
            test_scope = "pytest"
        elif "unittest" in harness_cmd_lower:
            test_scope = "unittest"
        elif "npm test" in harness_cmd_lower or "vitest" in harness_cmd_lower or "jest" in harness_cmd_lower:
            test_scope = "javascript_test"
        else:
            test_scope = "custom_harness"

        # ── 更新 last_good_snapshot ───────────────
        if harness_pass:
            last_good_snapshot = snapshot_tag
            print(f"[Git] last_good_snapshot 更新 → {snapshot_tag}")

        workflow_rollback_reason = "not_required"
        if sm.consecutive_fails >= 2 and not harness_pass:
            workflow_rollback_reason = "next_failure_will_trigger_rollback"
        if lines_changed == 0 and not harness_pass:
            workflow_rollback_reason = "no_effect_change"
        elif root_cause in {"syntax_error", "runtime_exception"}:
            workflow_rollback_reason = "runtime_regression_risk"
        elif harness_pass:
            workflow_rollback_reason = "validated_pass"

        # ── 記錄 Git Diff Intel ───────────────────
        patch_type = "bugfix" if sm.consecutive_fails > 0 else "feature"
        record_diff_intel(
            patch_type, changed_files, harness_result,
            harness_delta, lines_changed, rollback=False,
        )

        # ── Episode Scoring → L3 ─────────────────
        record_episode(
            task,
            harness_pass,
            diff_summary,
            harness_delta,
            files=candidate_files,
            iteration=iteration,
            max_iter=max_iter,
            aider_model=aider_model,
            harness_cmd=harness_cmd,
            work_dir=work_dir,
            prompt_excerpt=aider_message,
            harness_excerpt=last_harness_output[-1200:],
            selected_l4_ids=[str(item.get("id", "")) for item in selected_l4],
            selected_l5_ids=[str(item.get("id", "")) for item in selected_l5],
            patch_summary=patch_summary,
            task_complete=task_complete,
            run_id=run_id,
            trace_dir=run_trace_dir,
            failure_mode=failure_mode,
            root_cause=root_cause,
            patch_type=patch_type,
            repo_type=repo_type,
            language=language,
            framework=framework,
            test_scope=test_scope,
            workflow_task_type=workflow_plan["task_type"],
            workflow_complexity=workflow_plan["complexity"],
            workflow_mode=workflow_plan["execution_mode"],
            workflow_file_plan=summarize_file_plan(workflow_plan["file_plan"], limit=8),
            workflow_goal=workflow_plan["parsed_task"]["goal"],
            workflow_constraints=" | ".join(workflow_plan["parsed_task"]["constraints"]),
            workflow_acceptance=" | ".join(workflow_plan["parsed_task"]["acceptance"]),
            workflow_research_reason=workflow_plan["research_reason"],
            workflow_rollback_reason=workflow_rollback_reason,
            workflow_strategy_note=workflow_plan["strategy_note"],
        )

        save_trace_artifacts(
            run_trace_dir,
            iteration=iteration,
            event_type="action",
            payload={
                "run_id": run_id,
                "event_type": "action",
                "iteration": iteration,
                "state_before": sm.state,
                "task_complete": task_complete,
                "snapshot_tag": snapshot_tag,
                "selected_files": candidate_files,
                "selected_l4_ids": [str(item.get("id", "")) for item in selected_l4],
                "selected_l5_ids": [str(item.get("id", "")) for item in selected_l5],
                "diff_summary": diff_summary,
                "lines_changed": lines_changed,
                "harness_pass": harness_pass,
                "harness_delta": harness_delta,
                "failure_mode": failure_mode,
                "root_cause": root_cause,
                "repo_type": repo_type,
                "language": language,
                "framework": framework,
                "test_scope": test_scope,
                "workflow": {
                    "task_type": workflow_plan["task_type"],
                    "complexity": workflow_plan["complexity"],
                    "execution_mode": workflow_plan["execution_mode"],
                    "goal": workflow_plan["parsed_task"]["goal"],
                    "constraints": workflow_plan["parsed_task"]["constraints"][:6],
                    "acceptance": workflow_plan["parsed_task"]["acceptance"][:6],
                    "file_plan": workflow_plan["file_plan"][:8],
                    "research_reason": workflow_plan["research_reason"],
                    "rollback_reason": workflow_rollback_reason,
                    "strategy_note": workflow_plan["strategy_note"],
                },
            },
            prompt_text=aider_message,
            aider_output=aider_out,
            harness_output=last_harness_output,
            diff_text=diff_text,
        )

        # ── MemPalace 更新 ────────────────────────
        mempalace_context = (
            f"[GOAL]\n{task}\n\n"
            f"[DONE]\n迭代{iteration}：{'PASS' if harness_pass else 'FAIL'}\n"
            f"{''.join(f'- {l}{chr(10)}' for l in log_lines[-5:])}"
            f"\n[PENDING]\n{'後續迭代繼續' if not task_complete else '任務完成'}\n"
            f"\n[CONSTRAINTS]\n連敗{sm.consecutive_fails}次 / rollback{sm.rollback_count}次"
        )
        update_mempalace(mempalace_context)

        # ── 狀態機更新 ────────────────────────────
        new_state = sm.update(harness_pass, task_complete)
        final_task_complete = task_complete and harness_pass
        research_brief = ""   # 每次 Aider 執行後清除 brief

        log_line = (
            f"iter={iteration} {harness_result.upper()} "
            f"delta={harness_delta:+.1f} state={new_state}"
        )
        log_lines.append(log_line)
        print(f"[Loop] {log_line}")

        if new_state == "stop":
            break

    # ── 迴圈結束 ──────────────────────────────────
    print(f"\n{'═'*60}")
    print("  迴圈結束")
    print(f"  共執行 {iteration} 次迭代")
    print(f"  最終狀態：{sm.summary()}")
    print(f"{'═'*60}\n")

    # Bridge：MemPalace → L3
    final_context = "\n".join(log_lines)
    bridge_to_l3(f"任務：{task}\n執行記錄：\n{final_context}")
    print("[Loop] Bridge 完成，記憶已寫入 L3")
    result = {
        "run_id": run_id,
        "final_harness_pass": final_harness_pass,
        "task_complete": final_task_complete,
        "iterations": iteration,
        "state": sm.state,
        "rollback_count": sm.rollback_count,
        "trace_dir": str(run_trace_dir),
    }
    write_json_file(run_trace_dir / "result.json", result)
    print(f"[LoopResult] {json.dumps(result, ensure_ascii=False)}")
    return result


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    global OLLAMA_MODEL_GOVERNOR, OLLAMA_MODEL_RESEARCHER, OLLAMA_MODEL  # 必須在函式最頂端宣告

    parser = argparse.ArgumentParser(
        description="main_loop.py — AI System v3.0 主控迴圈"
    )
    parser.add_argument("--task",         required=True,  help="任務描述")
    parser.add_argument("--harness",      required=True,  help="驗證指令，e.g. 'pytest tests/'")
    parser.add_argument("--aider-model",  default="claude-3-5-sonnet-20241022",
                        help="Aider 使用的 LLM 模型")
    parser.add_argument("--aider-files",  default="",
                        help="Aider 要編輯的檔案（空格分隔）")
    parser.add_argument("--work-dir",     default=".",    help="工作目錄")
    parser.add_argument("--max-iter",     type=int, default=20, help="最大迭代次數")
    parser.add_argument("--score-pattern", default="",
                        help="從 Harness 輸出中提取分數的 regex，e.g. 'score=(\\d+\\.\\d+)'")
    parser.add_argument("--ollama-governor",    default=OLLAMA_MODEL_GOVERNOR,
                        help="Governor 模型（狀態機 / MemPalace，預設 qwen3.5:27b）")
    parser.add_argument("--ollama-researcher",  default=OLLAMA_MODEL_RESEARCHER,
                        help="Researcher 模型（Autoresearch，預設 qwen2.5:7b）")
    parser.add_argument("--dry-run",      action="store_true",
                        help="不實際執行 Aider（測試用）")

    args = parser.parse_args()

    OLLAMA_MODEL_GOVERNOR   = args.ollama_governor
    OLLAMA_MODEL_RESEARCHER = args.ollama_researcher
    OLLAMA_MODEL            = OLLAMA_MODEL_GOVERNOR

    aider_files = [f for f in args.aider_files.split() if f] if args.aider_files else []
    work_dir    = Path(args.work_dir).resolve()

    if not work_dir.exists():
        print(f"[ERROR] work-dir 不存在：{work_dir}")
        sys.exit(1)

    result = main_loop(
        task         = args.task,
        harness_cmd  = args.harness,
        aider_model  = args.aider_model,
        aider_files  = aider_files,
        work_dir     = work_dir,
        max_iter     = args.max_iter,
        score_pattern= args.score_pattern or None,
        dry_run      = args.dry_run,
    )
    sys.exit(0 if result["final_harness_pass"] else 1)


if __name__ == "__main__":
    main()
