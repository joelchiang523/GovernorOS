#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_loop.py — AI System v3.0 主控迴圈

Usage:
  python main_loop.py \\
    --task    "實作 RSI 超買超賣過濾器" \\
    --harness "pytest tests/ -q" \\
    [--aider-model ollama/qwen2.5-coder:14b] \\
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
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ── aider 執行檔路徑（保留作 fallback，主要流程已不依賴）──────
AIDER_BIN: str = (
    shutil.which("aider")
    or os.path.expanduser("~/.local/bin/aider")
)

# ── Harness Python 路徑（確保有 pytest，優先使用啟動本腳本的 Python）──
def _detect_python_exec() -> str:
    """找到帶有 pytest 的 python3 執行檔。
    優先使用 sys.executable（conda 環境），再嘗試 PATH 中的 python3。
    """
    candidates = [sys.executable]
    which_py = shutil.which("python3")
    if which_py and which_py != sys.executable:
        candidates.append(which_py)
    for py in candidates:
        try:
            r = subprocess.run(
                [py, "-c", "import pytest"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return sys.executable  # 最終 fallback

HARNESS_PYTHON: str = _detect_python_exec()

# ── Aider subprocess 超時（保留設定，DDI 超時另行管理）────────
AIDER_TIMEOUT: int = int(os.environ.get("AIDER_TIMEOUT", "1200"))   # 預設 20 分鐘

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
FAST_MODE   = os.environ.get("GOVERNOR_FAST_MODE", "0") == "1"

OLLAMA_URL = "http://localhost:11434/api/generate"

# ── 雙模型分工（與 dream_cycle.py 保持一致）──────────────
# Governor：狀態機判斷 / Harness 解讀 / MemPalace 治理
OLLAMA_MODEL_GOVERNOR   = os.environ.get(
    "OLLAMA_MODEL_GOVERNOR", "qwen3.5:27b"
)
# Researcher：Autoresearch 200字 brief（高服從、快速產出）
OLLAMA_MODEL_RESEARCHER = os.environ.get(
    "OLLAMA_MODEL_RESEARCHER",
    "qwen3.5:9b",   # 同家族 Governor（qwen3.5:27b），JSON 遵從性強，6.6GB 不與 Governor 搶 VRAM
)
OLLAMA_MODEL = OLLAMA_MODEL_GOVERNOR  # 向後相容
AIDER_MODEL_LOCAL = os.environ.get(
    "OLLAMA_MODEL_AIDER",
    "ollama/qwen3.5:27b",
)

WAKE_L4_MIN_CONF = 0.70
WAKE_L5_MIN_CONF = 0.75


# ─────────────────────────────────────────────
# Ollama 呼叫工具（DDI Pipeline 共用）
# ─────────────────────────────────────────────

def ollama_call(prompt: str, model: str = "", timeout: int = 300, keep_alive: Optional[str] = None) -> str:
    """呼叫本地 Ollama API，回傳模型回應字串。
    keep_alive="0" 表示呼叫完成後立即卸載模型（節省 VRAM，避免佔用影響下次呼叫）。
    """
    _model = model or OLLAMA_MODEL_RESEARCHER
    payload: dict = {"model": _model, "prompt": prompt, "stream": False}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    try:
        resp = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] 無法連接 Ollama（{OLLAMA_URL}），請確認 ollama serve 已啟動")
        return ""
    except requests.exceptions.Timeout:
        print(f"[ERROR] Ollama 呼叫逾時（{timeout}s），模型={_model}")
        return ""
    except Exception as e:
        print(f"[ERROR] Ollama 呼叫失敗：{e}")
        return ""


def parse_json_from_response(text: str) -> Optional[dict | list]:
    """從模型回應中提取 JSON（支援 ```json ... ``` 或裸 JSON）。"""
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    print(f"[WARN] 無法解析 JSON，原始回應片段：{text[:200]}")
    return None


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
            self.state = "stop"
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


def build_task_batches(
    aider_files: list[str],
    file_plan: list[dict],
    complexity: str,
) -> list[dict]:
    if not aider_files:
        return [{"label": "full_scope", "files": []}]

    ordered_files = [item["file"] for item in file_plan if item.get("file")] or list(aider_files)
    ordered_files = list(dict.fromkeys(ordered_files))
    if len(ordered_files) <= 3:
        return [{"label": "full_scope", "files": ordered_files}]

    batch_size = 2 if complexity == "high" or len(ordered_files) >= 8 else 3
    batches = []
    for idx in range(0, len(ordered_files), batch_size):
        chunk = ordered_files[idx: idx + batch_size]
        batches.append({
            "label": f"batch_{len(batches) + 1}",
            "files": chunk,
        })
    return batches


def select_iteration_files(
    iteration: int,
    task_batches: list[dict],
    fallback_files: list[str],
) -> tuple[list[str], str]:
    if not task_batches:
        return fallback_files, "full_scope"
    if len(task_batches) == 1:
        return task_batches[0]["files"], task_batches[0]["label"]

    batch_count = len(task_batches)
    if iteration <= batch_count:
        batch = task_batches[iteration - 1]
        return batch["files"], batch["label"]

    integration_step = iteration - batch_count
    merge_count = min(batch_count, 1 + integration_step)
    merged = []
    for batch in task_batches[:merge_count]:
        merged.extend(batch["files"])
    merged = list(dict.fromkeys(merged))
    return merged or fallback_files, f"integration_{merge_count}"


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
    active_files: list[str],
    active_label: str,
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
        "active_files": active_files,
        "active_label": active_label,
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
    # 確保 harness 使用有 pytest 的 Python（替換 python3/python 為 HARNESS_PYTHON）
    _hp = shlex.quote(HARNESS_PYTHON)
    _cmd_patched = re.sub(r'\bpython3\b', _hp, cmd, count=1)
    if _cmd_patched == cmd:
        _cmd_patched = re.sub(r'\bpython\b', _hp, cmd, count=1)
    if _cmd_patched != cmd:
        print(f"[Harness] python → {HARNESS_PYTHON}")
    cmd = _cmd_patched

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

def extract_local_editor_content(output: str, target_file: str) -> Optional[str]:
    tagged_patterns = [
        rf"<<FILE:{re.escape(target_file)}>>\s*\n?(.*?)\n?<<END_FILE>>",
        r"<<FILE>>\s*\n?(.*?)\n?<<END_FILE>>",
    ]
    for pattern in tagged_patterns:
        matches = re.findall(pattern, output, re.DOTALL)
        if matches:
            content = matches[-1]
            return content.lstrip("\n")

    fenced = re.findall(r"```(?:[\w.+-]+)?\n(.*?)```", output, re.DOTALL)
    if fenced:
        return fenced[-1].lstrip("\n")

    return None


def coerce_subprocess_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_local_qwen_editor(
    task_with_context: str,
    files: list[str],
    work_dir: Path,
    model: str,
) -> tuple[str, bool]:
    if len(files) != 1:
        return "[LocalQwenEditor] 單檔模式限定 1 個檔案", False

    target_file = files[0]
    target_path = work_dir / target_file
    if not target_path.exists():
        return f"[LocalQwenEditor] 找不到檔案：{target_file}", False

    try:
        original = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        original = target_path.read_text(encoding="utf-8", errors="replace")

    normalized_model = model.replace("ollama/", "", 1)
    prompt = (
        f"Edit this one file only: {target_file}\n"
        "Return the full updated file only.\n"
        f"Format:\n<<FILE:{target_file}>>\n...\n<<END_FILE>>\n"
        "No explanation.\n\n"
        f"[TASK]\n{task_with_context}\n\n"
        f"[FILE]\n{original}"
    )

    print(f"[LocalQwenEditor] 直接呼叫 Ollama API {normalized_model} 編輯 {target_file}")
    local_timeout = int(os.environ.get("LOCAL_QWEN_EDIT_TIMEOUT", "240"))
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": normalized_model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=min(AIDER_TIMEOUT, local_timeout),
        )
        resp.raise_for_status()
        output = resp.json().get("response", "")
        returncode = 0
    except requests.Timeout:
        print(f"[LocalQwenEditor] timeout={local_timeout}s，改從部分輸出擷取")
        output = ""
        returncode = 0
    except Exception as exc:
        output = coerce_subprocess_output(exc)
        returncode = 1

    extracted = extract_local_editor_content(output, target_file)

    if returncode != 0:
        print(f"[LocalQwenEditor] 執行異常（exit={returncode}）")
        lines = output.strip().splitlines()
        if lines:
            print("\n".join(lines[-10:]))
        return output, False

    if not extracted:
        print("[LocalQwenEditor] 無法從模型輸出擷取完整檔案內容")
        return output, False

    if extracted != original:
        target_path.write_text(extracted, encoding="utf-8")
        print(f"[LocalQwenEditor] 已寫入 {target_file}")
    else:
        print(f"[LocalQwenEditor] {target_file} 無差異")

    task_complete = bool(re.search(r'^\s*TASK_COMPLETE\s*$', output, re.MULTILINE))
    return output, task_complete


# ─────────────────────────────────────────────
# DDI Pipeline（Decompose → Draft → Integrate → Self-validate）
# 讓本地 qwen3.5 獨立分解複雜任務、逐段生成程式碼、整合後自我驗證
# ─────────────────────────────────────────────

def _find_test_file_content(work_dir: Path, target_file: str) -> str:
    """嘗試找到對應測試檔案並讀取（供 DDI 推斷函數簽名）"""
    stem = Path(target_file).stem
    candidates = [
        work_dir / "tests" / f"test_{stem}.py",
        work_dir / f"test_{stem}.py",
        work_dir / "tests" / f"{stem}_test.py",
        work_dir / f"{stem}_test.py",
    ]
    for c in candidates:
        if c.exists():
            try:
                return c.read_text(encoding="utf-8")[:1500]
            except Exception:
                pass
    return ""


def _extract_function_code(raw: str, function_name: str) -> str:
    """從模型輸出中提取指定函數的完整程式碼"""
    # 嘗試 markdown code block
    code_blocks = re.findall(r'```(?:python)?\n(.*?)```', raw, re.DOTALL)
    for block in reversed(code_blocks):
        if f"def {function_name}" in block:
            lines = block.splitlines()
            func_lines: list[str] = []
            in_func = False
            base_indent = 0
            for line in lines:
                stripped = line.lstrip()
                if not in_func:
                    if stripped.startswith(f"def {function_name}"):
                        in_func = True
                        base_indent = len(line) - len(stripped)
                        func_lines.append(line)
                else:
                    cur_indent = len(line) - len(line.lstrip()) if line.strip() else base_indent + 4
                    if line.strip() and cur_indent <= base_indent and stripped.startswith(("def ", "class ", "@")):
                        break
                    func_lines.append(line)
            while func_lines and not func_lines[-1].strip():
                func_lines.pop()
            if func_lines:
                return "\n".join(func_lines)
        elif block.strip().startswith("def "):
            return block.strip()

    # 直接在原始文本掃描
    lines = raw.splitlines()
    func_lines = []
    in_func = False
    base_indent = 0
    for line in lines:
        stripped = line.lstrip()
        if not in_func:
            if stripped.startswith(f"def {function_name}"):
                in_func = True
                base_indent = len(line) - len(stripped)
                func_lines.append(line)
        else:
            cur_indent = len(line) - len(line.lstrip()) if line.strip() else base_indent + 4
            if line.strip() and cur_indent <= base_indent and stripped.startswith(("def ", "class ", "@")):
                break
            func_lines.append(line)
    while func_lines and not func_lines[-1].strip():
        func_lines.pop()
    return "\n".join(func_lines) if func_lines else ""


def should_use_ddi(task: str, file_content: str, files: list[str]) -> bool:
    """
    判斷任務是否需要 DDI 分解。
    有多個函數目標、多個檔案、或現有多函數修正任務才需分解。
    """
    # 多個編號項目（1. 2. 3. 或 (1)(2)(3)）
    if re.search(r'(?:^|\n|\s)[（(]?[1-9][.、:）)]\s+\S', task):
        return True
    # 多個檔案
    if len(files) >= 2:
        return True
    # 明確提到多個函數（實作/修正 xxx() + 實作/修正 yyy()）
    func_keywords = re.findall(
        r'(?:實作|修正|修改|implement|fix)\s+\w+\s*\(',
        task, re.IGNORECASE,
    )
    if len(func_keywords) >= 2:
        return True
    # 現有檔案有 3+ 函數定義且任務是修正類
    defs = len(re.findall(r'^def\s+', file_content, re.MULTILINE))
    if defs >= 3 and any(kw in task for kw in ["修正", "修改", "fix", "修復"]):
        return True
    return False


def ddi_decompose(
    task: str,
    file_content: str,
    filename: str,
    test_content: str,
) -> list[dict]:
    """
    Stage 1（Researcher）：將任務分解為原子子任務 JSON 陣列。
    每個子任務只對應一個函數/方法，合計必須覆蓋整個任務需求。
    驗證：解析後確認所有任務要求的函數都有對應子任務。
    """
    existing_funcs = re.findall(r'^def\s+(\w+)', file_content, re.MULTILINE)
    func_list = ", ".join(existing_funcs[:12]) if existing_funcs else "（無）"

    prompt = f"""你是任務分解專家。仔細閱讀任務需求，列出所有需要新增或修正的函數，分解為原子子任務。

任務需求：{task}

目標檔案：{filename}
現有函數清單：{func_list}

測試案例（用於確認函數簽名，部分）：
{test_content[:900] if test_content else "（不可用）"}

輸出嚴格 JSON 陣列（確保覆蓋任務所有要求的函數）：
[
  {{
    "id": "sub_1",
    "function_name": "要新增或修正的函數名稱",
    "goal": "此函數的具體實作目標（50字以內）",
    "signature": "def function_name(param1, param2):",
    "dependencies": [],
    "acceptance": "驗收標準（20字以內）",
    "is_fix": false
  }}
]

規則：
- id 依序 sub_1, sub_2...
- is_fix=true 表示修正現有函數，false 表示新增
- dependencies 只填同陣列的其他 id
- 只輸出 JSON 陣列，禁止任何說明文字
"""

    raw = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER, timeout=300)
    parsed = parse_json_from_response(raw)
    if not parsed or not isinstance(parsed, list):
        print("[DDI-Decompose] 無法解析子任務，降級為單次生成")
        return []

    valid: list[dict] = []
    for item in parsed:
        fn = str(item.get("function_name", "")).strip()
        goal = str(item.get("goal", "")).strip()
        if fn and goal:
            valid.append({
                "id": item.get("id", f"sub_{len(valid)+1}"),
                "function_name": fn,
                "goal": goal[:160],
                "signature": str(item.get("signature", f"def {fn}(...):")),
                "dependencies": [str(d) for d in item.get("dependencies", [])],
                "acceptance": str(item.get("acceptance", "通過 harness 測試"))[:80],
                "is_fix": bool(item.get("is_fix", False)),
            })

    # ── 補全驗證：確保 test 檔中所有被呼叫的函數都有對應子任務 ──────────
    if test_content:
        # 從 test 檔抓出被 import 的函數名稱（from solution import f1, f2, ...）
        import_match = re.search(r'from\s+\w+\s+import\s+(.+)', test_content)
        if import_match:
            imported = [f.strip() for f in import_match.group(1).split(',')]
            covered = {s['function_name'] for s in valid}
            for fn in imported:
                if fn and fn not in covered:
                    # 從現有函數清單判斷是修正還是新增
                    is_fix = fn in existing_funcs
                    valid.append({
                        "id": f"sub_{len(valid)+1}",
                        "function_name": fn,
                        "goal": f"實作或修正 {fn}（由 test 補全）",
                        "signature": f"def {fn}(...):",
                        "dependencies": [],
                        "acceptance": "通過 harness 測試",
                        "is_fix": is_fix,
                    })
                    print(f"[DDI-Decompose] 補全缺漏子任務：{fn}")

    print(f"[DDI-Decompose] {len(valid)} 個子任務：{', '.join(s['function_name'] for s in valid)}")
    return valid


def ddi_draft_subtask(
    subtask: dict,
    file_content: str,
    completed_drafts: dict[str, str],
    extra_hint: str = "",
) -> str:
    """
    Stage 2（Researcher）：對單一子任務生成函數程式碼。
    每次只聚焦一個函數，context 精簡，減少模型混亂。
    """
    # 已完成函數的第一行（簽名），用於介面一致性
    completed_sigs = "\n".join(
        code.splitlines()[0]
        for code in completed_drafts.values()
        if code.strip()
    )[:400]

    hint_line = f"\n注意修正方向：{extra_hint}" if extra_hint else ""

    prompt = f"""只實作以下單一函數，不要輸出其他函數、import 語句或任何說明。

函數名稱：{subtask['function_name']}
實作目標：{subtask['goal']}
函數簽名：{subtask['signature']}
驗收標準：{subtask['acceptance']}{hint_line}

現有程式碼片段（介面參考，勿重複輸出）：
{file_content[:900]}

已完成其他函數簽名（介面一致性）：
{completed_sigs if completed_sigs else "（無）"}

輸出：直接以 def {subtask['function_name']}( 開頭的完整函數定義，無任何前後說明。
"""

    raw = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER, timeout=300)

    # 精確提取目標函數
    code = _extract_function_code(raw, subtask["function_name"])
    if not code:
        # fallback：取最後一個 code block
        blocks = re.findall(r'```(?:python)?\n(.*?)```', raw, re.DOTALL)
        code = blocks[-1].strip() if blocks else raw.strip()

    print(f"[DDI-Draft] {subtask['function_name']}: {len(code.splitlines())} 行")
    return code


def ddi_integrate(
    task: str,
    file_content: str,
    filename: str,
    subtasks: list[dict],
    drafts: dict[str, str],
) -> str:
    """
    Stage 3（Governor）：將所有子任務草稿整合為完整、可執行的目標檔案。
    處理 import、函數順序、命名衝突，保留原始非任務相關程式碼。
    """
    drafts_text = "\n\n".join(
        f"### [{s['id']}] {s['function_name']} ###\n{drafts.get(s['id'], '(未生成，保留原有實作)')}"
        for s in subtasks
    )

    prompt = f"""你是程式整合專家。將各函數草稿整合成完整、可直接執行的 Python 檔案。

任務：{task}
目標檔案：{filename}

原始檔案（保留非任務相關程式碼）：
{file_content[:1800]}

各函數草稿（全部需整合進最終檔案）：
{drafts_text[:3500]}

整合規則：
1. 輸出完整的 {filename} 內容（從第一行到最後一行）
2. 所有必要 import 放置於檔案開頭
3. 保留原始非任務相關的 class、常數、helper 函數
4. 將各草稿函數整合（修正縮排、命名衝突）
5. 依賴關係排序：被呼叫的函數定義在前
6. 同名函數用草稿版本取代原有版本
7. 只輸出純 Python 程式碼，禁止輸出說明或 markdown fence（```）
"""

    # keep_alive="0"：整合完成後立即卸載 Governor，讓 Researcher(9b) 不必等 5 分鐘 VRAM 釋放
    raw = ollama_call(prompt, model=OLLAMA_MODEL_GOVERNOR, timeout=300, keep_alive="0")

    # 去除 markdown fence
    blocks = re.findall(r'```(?:python)?\n(.*?)```', raw, re.DOTALL)
    integrated = blocks[-1].strip() if blocks else raw.strip()

    if not integrated:
        print("[DDI-Integrate] 整合輸出為空")
        return ""

    print(f"[DDI-Integrate] 完成：{len(integrated.splitlines())} 行")
    return integrated


def ddi_self_validate(
    task: str,
    integrated_code: str,
    subtasks: list[dict],
    test_content: str,
) -> dict:
    """
    Stage 4（Researcher）：harness 執行前的自我驗證。
    確認整合後程式碼覆蓋所有需求、無明顯錯誤，
    回傳失敗子任務 id 供針對性重試。
    """
    subtask_list = "\n".join(
        f"- {s['function_name']}: {s['goal']} → 驗收: {s['acceptance']}"
        for s in subtasks
    )

    prompt = f"""你是程式碼審核員。逐一確認整合後程式碼是否符合所有任務需求。

原始任務：{task}

需要實作的函數清單：
{subtask_list}

整合後程式碼：
{integrated_code[:2500]}

測試案例提示（函數簽名參考）：
{test_content[:600] if test_content else "（不可用）"}

審核項目（逐一確認）：
1. 每個函數是否存在於程式碼中（搜尋 "def function_name"）
2. 函數簽名是否與需求匹配
3. 是否有明顯邏輯錯誤（空函數體只有 pass、缺少 return、無限迴圈）
4. import 是否完整

只輸出 JSON（禁止任何說明）：
{{
  "pass": true,
  "confidence": 0.90,
  "issues": ["具體問題描述（無問題時為空陣列）"],
  "failing_subtasks": ["需要重試的子任務 id（無問題時為空陣列）"],
  "summary": "一句話審核摘要"
}}
"""

    raw = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER, timeout=300)
    result = parse_json_from_response(raw)

    if not result or not isinstance(result, dict):
        print("[DDI-Validate] 回應解析失敗，假設通過")
        return {"pass": True, "confidence": 0.5, "issues": [], "failing_subtasks": [], "summary": "解析失敗"}

    passed = bool(result.get("pass", True))
    conf = float(result.get("confidence", 0.5))
    issues = result.get("issues", [])
    status_str = "PASS" if passed else f"FAIL（{len(issues)} 個問題）"
    print(f"[DDI-Validate] {status_str}  confidence={conf:.2f}")
    for issue in issues[:4]:
        print(f"  問題: {issue}")
    return result


def _single_shot_generate(task: str, file_content: str, filename: str, model: str) -> str:
    """
    Fallback：單次呼叫生成整個檔案。
    適用於簡單單函數任務，或 DDI 分解/整合失敗時的保底機制。
    """
    prompt = (
        f"修正或實作以下程式任務，輸出完整的 {filename} 檔案。\n"
        f"任務：{task}\n\n"
        f"當前檔案內容：\n{file_content[:2000]}\n\n"
        "輸出規則：\n"
        "1. 直接輸出完整 Python 程式碼\n"
        "2. 不要輸出說明、注釋或任何非程式碼文字\n"
        "3. 不要輸出 markdown fence（```）\n"
        "4. 從第一個 import 或 def 或 class 開始"
    )
    raw = ollama_call(prompt, model=model, timeout=300)
    blocks = re.findall(r'```(?:python)?\n(.*?)```', raw, re.DOTALL)
    return blocks[-1].strip() if blocks else raw.strip()


def run_local_ollama_ddi(
    task_with_context: str,
    files: list[str],
    work_dir: Path,
    model: str,
) -> tuple[str, bool]:
    """
    DDI Pipeline 主函式：Decompose → Draft → Integrate → Self-validate
    ─────────────────────────────────────────────────────────────────
    • Stage 1  Researcher 分解任務為原子子任務（每子任務 = 一個函數）
    • Stage 2  Researcher 逐子任務生成程式碼（小 context、精準輸出）
    • Stage 3  Governor 將所有草稿整合為完整檔案
    • Stage 4  Researcher 自我驗證是否符合需求，不過則針對性重試

    取代外部 aider binary，支援所有本地 ollama 模型與多檔案任務。
    task_complete 永遠回傳 False，由後續 harness 結果決定成敗。
    """
    normalized_model = model.replace("ollama/", "", 1)
    output_log: list[str] = []
    any_changed = False

    # 從帶有 context 的完整訊息中提取核心任務描述
    core_task = task_with_context
    m = re.search(r'任務[：:]\s*(.+?)(?:\n|驗收|本輪|$)', task_with_context, re.DOTALL)
    if m:
        core_task = m.group(1).strip()[:500]
    if not core_task or len(core_task) < 10:
        core_task = task_with_context[:500]

    for target_file in files:
        target_path = work_dir / target_file

        # 讀取現有檔案內容
        if target_path.exists():
            try:
                file_content = target_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                file_content = target_path.read_text(encoding="utf-8", errors="replace")
        else:
            file_content = ""
            target_path.parent.mkdir(parents=True, exist_ok=True)

        test_content = _find_test_file_content(work_dir, target_file)
        print(f"\n[DDI] 處理檔案：{target_file}（{len(file_content.splitlines())} 行）")

        # ── 決定是否需要分解 ────────────────────────────────────
        use_decompose = should_use_ddi(core_task, file_content, [target_file])
        print(f"[DDI] 模式：{'DDI分解生成' if use_decompose else '單次生成（簡單任務）'}")

        integrated_code = ""
        subtasks: list[dict] = []

        if use_decompose:
            # Stage 1: Decompose
            print("[DDI] Stage 1: 任務分解...")
            subtasks = ddi_decompose(core_task, file_content, target_file, test_content)

        if subtasks:
            # Stage 2: Draft（按依賴順序執行）
            print(f"[DDI] Stage 2: 逐子任務生成（共 {len(subtasks)} 個）...")
            completed_drafts: dict[str, str] = {}

            # 拓撲排序：依賴解決後才執行
            ordered: list[dict] = []
            remaining = list(subtasks)
            seen_ids: set[str] = set()
            for _ in range(len(remaining) * 2 + 1):
                if not remaining:
                    break
                for sub in list(remaining):
                    if all(d in seen_ids for d in sub.get("dependencies", [])):
                        ordered.append(sub)
                        remaining.remove(sub)
                        seen_ids.add(sub["id"])
            ordered.extend(remaining)  # 循環依賴或未解的直接加入

            for subtask in ordered:
                draft = ddi_draft_subtask(subtask, file_content, completed_drafts)
                if draft:
                    completed_drafts[subtask["id"]] = draft
                else:
                    print(f"[DDI]   {subtask['function_name']}: 草稿空輸出，繼續")

            if completed_drafts:
                # Stage 3: Integrate
                print(f"[DDI] Stage 3: Governor 整合（{len(completed_drafts)}/{len(subtasks)} 草稿）...")
                integrated_code = ddi_integrate(
                    core_task, file_content, target_file, subtasks, completed_drafts
                )

                if integrated_code:
                    # Stage 4: Self-validate
                    print("[DDI] Stage 4: 自我驗證...")
                    validation = ddi_self_validate(
                        core_task, integrated_code, subtasks, test_content
                    )

                    failing = [
                        f for f in validation.get("failing_subtasks", [])
                        if any(s["id"] == f for s in subtasks)
                    ]

                    # 針對性重試（最多重試 2 個子任務、1 輪）
                    if not validation.get("pass") and failing:
                        issues_hint = " | ".join(
                            str(i) for i in validation.get("issues", [])[:3]
                        )
                        print(f"[DDI] 驗證未通過，針對性重試 {len(failing[:2])} 個子任務...")

                        for retry_id in failing[:2]:
                            retry_sub = next(
                                (s for s in subtasks if s["id"] == retry_id), None
                            )
                            if retry_sub:
                                new_draft = ddi_draft_subtask(
                                    retry_sub,
                                    integrated_code,   # 用整合後的程式碼作為 context
                                    {k: v for k, v in completed_drafts.items()
                                     if k != retry_id},
                                    extra_hint=issues_hint[:100],
                                )
                                if new_draft:
                                    completed_drafts[retry_id] = new_draft

                        # 重新整合（含修正後草稿）
                        print("[DDI] 重新整合（含修正版草稿）...")
                        integrated_code = ddi_integrate(
                            core_task, file_content, target_file, subtasks, completed_drafts
                        )
            else:
                print("[DDI] Stage 2 全部空輸出，降級為單次生成")

        # Fallback：subtasks 為空 或 整合失敗
        if not integrated_code:
            reason = "分解 fallback" if subtasks else "簡單任務"
            print(f"[DDI] 單次生成（{reason}）...")
            integrated_code = _single_shot_generate(
                core_task, file_content, target_file, normalized_model
            )

        # ── 寫入目標檔案 ────────────────────────────────────────
        if integrated_code and integrated_code.strip():
            if integrated_code.strip() != file_content.strip():
                target_path.write_text(integrated_code, encoding="utf-8")
                lines = len(integrated_code.splitlines())
                print(f"[DDI] 已寫入 {target_file}（{lines} 行）")
                output_log.append(
                    f"[DDI] {target_file}: 已更新（{len(subtasks)} 個子任務，{lines} 行）"
                )
                any_changed = True
            else:
                print(f"[DDI] {target_file}: 無差異，未寫入")
                output_log.append(f"[DDI] {target_file}: 無差異")
        else:
            print(f"[DDI] {target_file}: 無法生成有效程式碼")
            output_log.append(f"[DDI] {target_file}: 生成失敗")

    full_output = "\n".join(output_log) if output_log else "[DDI] 完成（無輸出）"
    # task_complete 永遠 False：harness 結果是唯一成敗判準
    return full_output, False


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

    def normalize_aider_model(raw_model: str) -> str:
        """
        GovernorOS 執行期只使用本地 Aider 模型。
        歷史上保留下來的 claude/openai 名稱只作訓練監督標記，不應在 runtime 觸發 API 呼叫。
        """
        model_name = (raw_model or "").strip()
        if not model_name or "/" in model_name:
            return model_name or AIDER_MODEL_LOCAL

        anthropic_prefixes = (
            "claude-",
            "sonnet",
            "haiku",
            "opus",
        )
        openai_prefixes = (
            "gpt-",
            "o1",
            "o3",
            "o4",
        )
        gemini_prefixes = ("gemini",)
        deepseek_prefixes = ("deepseek",)

        lowered = model_name.lower()
        if (
            lowered.startswith(anthropic_prefixes)
            or lowered.startswith(openai_prefixes)
            or lowered.startswith(gemini_prefixes)
            or lowered.startswith(deepseek_prefixes)
        ):
            print(
                f"[Aider] runtime 偵測到雲端模型標記 {model_name}，"
                f"自動改用本地模型 {AIDER_MODEL_LOCAL}"
            )
            return AIDER_MODEL_LOCAL
        return model_name

    normalized_model = normalize_aider_model(model)

    # 所有本地 ollama 模型統一走 DDI Pipeline（移除外部 aider binary 依賴）
    if normalized_model.startswith("ollama/"):
        return run_local_ollama_ddi(task_with_context, files, work_dir, normalized_model)

    # ── 非 ollama 模型（保留 aider binary 路徑，作為雲端模型後備）──
    weak_model = ""
    if normalized_model.startswith("ollama/"):
        weak_model = normalized_model

    file_args = " ".join(shlex.quote(f) for f in files) if files else ""
    message = shlex.quote(task_with_context)
    weak_model_arg = f"--weak-model {shlex.quote(weak_model)} " if weak_model else ""

    cmd = (
        f'"{AIDER_BIN}" --yes-always --no-auto-commits '
        f'--no-detect-urls --no-auto-lint --map-tokens 0 '
        f'--no-browser --no-show-model-warnings '
        f'--model {shlex.quote(normalized_model)} '
        f'{weak_model_arg}'
        f'{file_args} '
        f'--message {message}'
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
    if FAST_MODE and any(flag in args for flag in ("--update-mempalace", "--bridge")):
        print(f"[DreamCycle] fast mode skip: {' '.join(args[:2])}")
        return
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


def recall_past_failures(task: str, similarity_threshold: float = 0.3) -> str:
    """
    呼叫 dream_cycle.py --recall 取得類似過去失敗案例，
    若有結果且相似度 > threshold，回傳記憶回溯文字；否則回傳空字串。
    FAST_MODE 下略過（避免拖慢 benchmark）。
    """
    if FAST_MODE:
        return ""
    try:
        result = subprocess.run(
            [sys.executable, str(DREAM_CYCLE), "--recall", f"--task={task[:200]}"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        records = json.loads(result.stdout.strip())
        if not isinstance(records, list):
            return ""
        # 只保留失敗且相似度 > threshold 的案例
        failures = [
            r for r in records
            if not r.get("harness_pass", True)
            and float(r.get("similarity", 0)) > similarity_threshold
        ]
        if not failures:
            return ""
        lines = ["[記憶回溯] 類似任務的過去失敗案例："]
        for r in failures:
            task_excerpt = r.get("task", "")[:60]
            failure_mode = r.get("failure_mode", "")
            root_cause = r.get("root_cause", "")
            lines.append(f"- {task_excerpt}: {failure_mode} → {root_cause}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[Recall] 略過（{e}）")
        return ""


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
    # 用 lambda 避免 replacement 含 \d \1 等被當 regex template 解析
    updated = pattern.sub(lambda m: replacement, content) if pattern.search(content) else (
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
    base_task_type = infer_task_type(task, harness_cmd, aider_files)
    base_complexity = infer_task_complexity(task, aider_files, max_iter)
    base_file_plan = build_file_plan(task, aider_files, base_task_type)
    task_batches = build_task_batches(aider_files, base_file_plan, base_complexity)
    if len(task_batches) > 1:
        batch_summary = " | ".join(
            f"{batch['label']}={','.join(batch['files'])}"
            for batch in task_batches
        )
        print(f"[TaskSplit] 啟動分段策略：{batch_summary}")
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
        active_files, active_label = select_iteration_files(iteration, task_batches, aider_files)
        print(f"[TaskSplit] 本輪範圍：{active_label} -> {', '.join(active_files) if active_files else '(auto)'}")
        selected_l4, selected_l5 = select_relevant_memories(task, active_files)
        # ── Total Recall：注入過去相似失敗案例 ────────────
        recall_context = recall_past_failures(task) if iteration == 1 else ""
        if recall_context:
            print(f"[Recall] 找到相似失敗案例，已注入 context")
        workflow_plan = build_workflow_plan(
            task,
            harness_cmd,
            active_files,
            max_iter,
            sm.consecutive_fails,
            research_brief,
            last_harness_output,
            selected_l4,
            selected_l5,
            active_files,
            active_label,
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
        is_local_qwen_aider = AIDER_MODEL_LOCAL.startswith("ollama/qwen3.5")
        if is_local_qwen_aider:
            compact_failure = sanitize_cli_text(last_harness_output[-700:], 700)
            compact_research = sanitize_cli_text(research_brief, 220)
            aider_message = (
                f"[ITER {iteration}/{max_iter}] 任務：{workflow_plan['parsed_task']['goal']}\n"
                f"本輪範圍：{workflow_plan['active_label']} -> {', '.join(workflow_plan['active_files']) or '(none)'}\n"
                f"驗收：{'; '.join(workflow_plan['parsed_task']['acceptance']) or harness_cmd}\n"
                + (f"{recall_context}\n" if recall_context else "")
                + (f"研究提示：{compact_research}\n" if compact_research else "")
                + (f"當前失敗：{compact_failure}\n" if compact_failure else "")
                + "直接修改提供的檔案。\n"
                + "不要輸出說明、不要輸出步驟、不要輸出 markdown 標題、不要輸出 code fence。\n"
                + "只輸出 Aider 可套用的 whole-file 修改內容；未完成時不要輸出 TASK_COMPLETE。\n"
            )
        else:
            aider_message = (
                f"[ITER {iteration}/{max_iter}] 任務：{task}\n"
                + (f"\n{recall_context}\n" if recall_context else "")
                + (
                    f"\n[Workflow Plan]\n"
                    f"- task_type={workflow_plan['task_type']}\n"
                    f"- complexity={workflow_plan['complexity']}\n"
                    f"- mode={workflow_plan['execution_mode']}\n"
                    f"- goal={workflow_plan['parsed_task']['goal']}\n"
                    f"- active_scope={workflow_plan['active_label']}\n"
                    f"- active_files={', '.join(workflow_plan['active_files']) or '(none)'}\n"
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
            aider_message, active_files, work_dir, aider_model, dry_run
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
        candidate_files = changed_files or active_files or aider_files

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
    parser.add_argument("--aider-model",  default=AIDER_MODEL_LOCAL,
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
                        help="Researcher 模型（DDI 分解/草稿/自我驗證，預設 qwen3.5:9b）")
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
