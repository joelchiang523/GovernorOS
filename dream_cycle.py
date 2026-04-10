#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dream_cycle.py
五層記憶系統核心引擎

功能：
  --init              初始化所有記憶檔案與目錄
  --record            記錄 L3 事件（附 Episode Importance Scoring）
  --update-mempalace  壓縮當前 context 更新 CLAUDE.md 四區
  --sleep             L3 驗證池 → L4 知識萃取（淺眠）
  --deep              L4 → L5 策略固化（深眠）
  --wake              注入 L4+L5 到 CLAUDE.md + PROMPT.md（醒來）
  --decay             執行 Memory Decay 衰減檢查
  --bridge            MemPalace 輸出 → L3 格式轉換
  --status            顯示當前記憶系統狀態
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
MEMORY_DIR = BASE_DIR / "memory"
CLAUDE_MD = BASE_DIR / "CLAUDE.md"
PROMPT_MD = BASE_DIR / "PROMPT.md"

PATHS = {
    "L1": MEMORY_DIR / "L1_buffer.txt",
    "L2": MEMORY_DIR / "L2_working.md",
    "L3": MEMORY_DIR / "L3_episodes.jsonl",
    "L4": MEMORY_DIR / "L4_knowledge.json",
    "L5": MEMORY_DIR / "L5_strategies.json",
    "WAKE_HISTORY": MEMORY_DIR / "wake_history.jsonl",
    "STRATEGY_HISTORY": MEMORY_DIR / "strategy_history.jsonl",
    "BENCHMARK_HISTORY": MEMORY_DIR / "benchmark_history.jsonl",
}
EXPORT_DIR = BASE_DIR / "exports"
TRACE_ROOT = MEMORY_DIR / "traces"

OLLAMA_URL = "http://localhost:11434/api/generate"

# ── 雙模型分工 ────────────────────────────────────────────
# Governor：主控 / 判斷 / 治理 / 複核（穩定、規則感強）
OLLAMA_MODEL_GOVERNOR   = os.environ.get(
    "OLLAMA_MODEL_GOVERNOR", "qwen3.5:27b"
)
# Researcher：生成 / 萃取 / 草稿 / Bridge（高服從、快速產出）
OLLAMA_MODEL_RESEARCHER = os.environ.get(
    "OLLAMA_MODEL_RESEARCHER",
    "qwen2.5:7b",
)
# 向後相容：ollama_call() 預設值沿用 Governor
OLLAMA_MODEL = OLLAMA_MODEL_GOVERNOR

# 門檻設定
THRESHOLDS = {
    "episode_min_score":      6,      # L3 進驗證池最低分
    "l3_to_l4_min_count":     3,      # 同類規律出現幾次才升 L4
    "l4_to_l5_min_conf":      0.75,   # L4 升 L5 最低 confidence
    "wake_l4_min_conf":       0.70,   # 注入 PROMPT 的 L4 最低 confidence
    "wake_l5_min_conf":       0.75,   # 注入 PROMPT 的 L5 最低 confidence
    "decay_days":             30,     # 幾天未驗證開始衰減
    "decay_amount":           0.05,   # 每週衰減量
    "inactive_threshold":     0.50,   # confidence 低於此值 → inactive
    "retire_days":            180,    # inactive 幾天後 retire
    "fail_ratio_threshold":   0.40,   # fail_count/total 超過此值 → L5 降級
    "conflict_overlap":       0.50,   # condition 重疊度超過此值視為衝突
    "max_wake_tokens":        500,    # 注入內容最大字數
    "decay_warning_days":     7,      # decay_timer 剩餘天數警告
    "strategy_promote_ratio": 0.70,   # strategy scorer 提升門檻
    "strategy_demote_ratio":  0.45,   # strategy scorer 降權門檻
    "strategy_min_uses":      3,      # strategy scorer 最低樣本數
}

# ─────────────────────────────────────────────
# Cost Governor
# ─────────────────────────────────────────────

TOKEN_BUDGET      = 500_000   # 單次 session 所有 Ollama 呼叫的 token 上限
MAX_DEEP_EPISODES = 20        # --deep 最多處理幾條 L4（按 confidence 降序截斷）
MAX_SLEEP_GROUPS  = 10        # --sleep 最多處理幾個事件群組

_session_tokens_used: int = 0  # session 累計 token 使用量


def estimate_tokens(text: str) -> int:
    """粗估 token 數：ASCII 約 4 chars/token，CJK 約 1.5 chars/token"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars   = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 1.5)


def budget_check(prompt: str, label: str = "") -> bool:
    """
    檢查加上本次 prompt 是否會超出 TOKEN_BUDGET。
    超出則印警告並回傳 False（呼叫方應跳過該 Ollama 呼叫）。
    未超出則累計使用量並回傳 True。
    """
    global _session_tokens_used
    estimated = estimate_tokens(prompt)
    after = _session_tokens_used + estimated
    if after > TOKEN_BUDGET:
        print(
            f"[CostGovernor] ⛔ budget 超出！"
            f"已用={_session_tokens_used:,}  本次≈{estimated:,}  "
            f"上限={TOKEN_BUDGET:,}  ({label})"
        )
        return False
    _session_tokens_used = after
    remaining = TOKEN_BUDGET - after
    print(
        f"[CostGovernor] token 使用 {after:,}/{TOKEN_BUDGET:,}"
        f"（+{estimated:,} {label}，剩餘 {remaining:,}）"
    )
    return True


# ─────────────────────────────────────────────
# Ollama 呼叫
# ─────────────────────────────────────────────

def ollama_call(prompt: str, model: str = OLLAMA_MODEL, timeout: int = 300) -> str:
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("[ERROR] 無法連接 Ollama，請確認服務已啟動（ollama serve）")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("[ERROR] Ollama 呼叫逾時")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Ollama 呼叫失敗：{e}")
        sys.exit(1)


def parse_json_from_response(text: str) -> Optional[dict | list]:
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
    print(f"[WARN] 無法解析 JSON，原始回應：\n{text[:300]}")
    return None


# ─────────────────────────────────────────────
# 記憶檔案 I/O
# ─────────────────────────────────────────────

def load_json(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return []
        return json.loads(content)


def save_json(path: Path, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_md(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_md(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def replace_block(content: str, start_tag: str, end_tag: str, new_body: str) -> str:
    pattern = re.compile(
        re.escape(start_tag) + r".*?" + re.escape(end_tag),
        re.DOTALL,
    )
    replacement = f"{start_tag}\n{new_body}\n{end_tag}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    return content + f"\n{replacement}\n"


def gen_id(prefix: str, path: Path) -> str:
    records = load_jsonl(path) if str(path).endswith(".jsonl") else load_json(path)
    return f"{prefix}_{len(records) + 1:03d}"


def sanitize_text(text: str, limit: int = 1500) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def infer_repo_type(framework: str, files: list[str], task: str) -> str:
    if framework in {"pygame", "godot", "unity"}:
        return "game"
    blob = " ".join(files).lower() + " " + task.lower()
    if any(token in blob for token in ["pygame", "godot", "unity", ".gd", "project.godot"]):
        return "game"
    return "general"


def infer_framework(files: list[str], task: str, harness_cmd: str, work_dir: str) -> str:
    blob = " ".join(files).lower()
    task_blob = task.lower()
    wd = work_dir.lower()
    hc = harness_cmd.lower()
    if "pygame" in blob or "pygame" in task_blob:
        return "pygame"
    if ".gd" in blob or "project.godot" in blob or "godot" in task_blob or "godot" in wd:
        return "godot"
    if ".cs" in blob and ("assets/" in wd or "projectsettings" in wd or "unity" in task_blob):
        return "unity"
    if "pytest" in hc and any(path.endswith(".py") for path in files):
        return "python"
    if any(path.endswith((".tsx", ".jsx")) for path in files):
        return "react"
    return "generic"


def infer_language(files: list[str]) -> str:
    suffixes = {Path(path).suffix.lower() for path in files if Path(path).suffix}
    if ".py" in suffixes:
        return "python"
    if ".gd" in suffixes:
        return "gdscript"
    if ".cs" in suffixes:
        return "csharp"
    if ".ts" in suffixes or ".tsx" in suffixes:
        return "typescript"
    if ".js" in suffixes or ".jsx" in suffixes:
        return "javascript"
    return "unknown"


def infer_test_scope(harness_cmd: str) -> str:
    lower = harness_cmd.lower()
    if "pytest" in lower:
        return "pytest"
    if "unittest" in lower:
        return "unittest"
    if "jest" in lower or "vitest" in lower or "npm test" in lower:
        return "javascript_test"
    if "godot" in lower:
        return "godot_test"
    return "custom_harness"


def infer_failure_taxonomy(status: int, harness_excerpt: str, diff_summary: str) -> tuple[str, str]:
    if status == 0:
        return "success", "validated_pass"

    text = harness_excerpt.lower()
    if "lines=0" in diff_summary:
        return "no_effect_change", "model_output_did_not_change_repo"
    if any(token in text for token in ["syntaxerror", "indentationerror", "parseerror"]):
        return "runtime_failure", "syntax_error"
    if any(token in text for token in ["importerror", "modulenotfounderror", "no module named"]):
        return "tooling_failure", "import_or_dependency_error"
    if any(token in text for token in ["assertionerror", "assert ", "expected", " != ", " == "]):
        return "test_failure", "assertion_failure"
    if "timeout" in text:
        return "runtime_failure", "timeout"
    if any(token in text for token in ["attributeerror", "typeerror", "keyerror", "valueerror", "indexerror", "nameerror"]):
        return "runtime_failure", "runtime_exception"
    if any(token in text for token in ["not found", "no such file", "filenotfounderror"]):
        return "tooling_failure", "missing_file"
    return "test_failure", "unknown_failure"


def infer_patch_type(diff_summary: str, files: list[str], task: str) -> str:
    lower = " ".join([diff_summary, task, ",".join(files)]).lower()
    if "test" in lower:
        return "test"
    if "config" in lower or ".json" in lower or ".yaml" in lower:
        return "config"
    if any(token in lower for token in ["fix", "bug", "error", "repair", "修正"]):
        return "bugfix"
    if any(token in lower for token in ["refactor", "cleanup", "rename", "重構"]):
        return "refactor"
    if any(token in lower for token in ["add", "create", "implement", "新增", "建立", "實作"]):
        return "feature"
    return "other"


def update_strategy_effectiveness(record: dict) -> None:
    status = int(record.get("status", 1))
    selected_l4_ids = record.get("context", {}).get("selected_l4_ids", [])
    selected_l5_ids = record.get("context", {}).get("selected_l5_ids", [])

    if not selected_l4_ids and not selected_l5_ids:
        return

    today = datetime.now().strftime("%Y-%m-%d")

    l4 = load_json(PATHS["L4"])
    for item in l4:
        if item.get("id") in selected_l4_ids:
            item["use_count"] = int(item.get("use_count", 0)) + 1
            if status == 0:
                item["pass_count"] = int(item.get("pass_count", 0)) + 1
            else:
                item["fail_count"] = int(item.get("fail_count", 0)) + 1
            item["last_selected"] = today
    save_json(PATHS["L4"], l4)

    l5 = load_json(PATHS["L5"])
    for item in l5:
        if item.get("id") in selected_l5_ids:
            item["use_count"] = int(item.get("use_count", 0)) + 1
            if status == 0:
                item["success_count"] = int(item.get("success_count", 0)) + 1
            else:
                item["fail_count"] = int(item.get("fail_count", 0)) + 1
            item["last_selected"] = today
    save_json(PATHS["L5"], l5)

    append_jsonl(PATHS["STRATEGY_HISTORY"], {
        "time": datetime.now().isoformat(),
        "episode_id": record.get("id"),
        "run_id": record.get("run_id", ""),
        "task": record.get("task", ""),
        "status": status,
        "selected_l4_ids": selected_l4_ids,
        "selected_l5_ids": selected_l5_ids,
        "repo_type": record.get("taxonomy", {}).get("repo_type", ""),
        "framework": record.get("taxonomy", {}).get("framework", ""),
    })


def compute_effectiveness(item: dict, success_key: str, fail_key: str) -> tuple[int, int, float]:
    success = int(item.get(success_key, 0))
    fail = int(item.get(fail_key, 0))
    total = success + fail
    ratio = (success / total) if total else 0.0
    return success, fail, ratio


def cmd_score_strategies() -> None:
    print("[StrategyScorer] 開始評估 L4/L5 effectiveness...")
    updates = {"l4_promoted": [], "l4_demoted": [], "l5_promoted": [], "l5_demoted": [], "l5_inactive": []}

    l4 = load_json(PATHS["L4"])
    for item in l4:
        success, fail, ratio = compute_effectiveness(item, "pass_count", "fail_count")
        total = success + fail
        if total < THRESHOLDS["strategy_min_uses"]:
            continue
        old_conf = float(item.get("confidence", 0.0))
        if ratio >= THRESHOLDS["strategy_promote_ratio"]:
            item["confidence"] = round(min(0.95, old_conf + 0.03), 3)
            item["last_scored"] = datetime.now().isoformat()
            item["score_note"] = f"promoted ratio={ratio:.2f} total={total}"
            updates["l4_promoted"].append(item["id"])
            print(f"  [L4↑] {item['id']} {old_conf:.2f} -> {item['confidence']:.2f} ratio={ratio:.0%}")
        elif ratio <= THRESHOLDS["strategy_demote_ratio"]:
            item["confidence"] = round(max(0.30, old_conf - 0.04), 3)
            item["last_scored"] = datetime.now().isoformat()
            item["score_note"] = f"demoted ratio={ratio:.2f} total={total}"
            updates["l4_demoted"].append(item["id"])
            print(f"  [L4↓] {item['id']} {old_conf:.2f} -> {item['confidence']:.2f} ratio={ratio:.0%}")
    save_json(PATHS["L4"], l4)

    l5 = load_json(PATHS["L5"])
    for item in l5:
        success, fail, ratio = compute_effectiveness(item, "success_count", "fail_count")
        total = success + fail
        if total < THRESHOLDS["strategy_min_uses"]:
            continue
        old_conf = float(item.get("confidence", 0.0))
        if ratio >= THRESHOLDS["strategy_promote_ratio"]:
            item["confidence"] = round(min(0.98, old_conf + 0.04), 3)
            item["last_scored"] = datetime.now().isoformat()
            item["score_note"] = f"promoted ratio={ratio:.2f} total={total}"
            updates["l5_promoted"].append(item["id"])
            print(f"  [L5↑] {item['id']} {old_conf:.2f} -> {item['confidence']:.2f} ratio={ratio:.0%}")
        elif ratio <= THRESHOLDS["strategy_demote_ratio"]:
            item["confidence"] = round(max(0.25, old_conf - 0.05), 3)
            item["last_scored"] = datetime.now().isoformat()
            item["score_note"] = f"demoted ratio={ratio:.2f} total={total}"
            updates["l5_demoted"].append(item["id"])
            print(f"  [L5↓] {item['id']} {old_conf:.2f} -> {item['confidence']:.2f} ratio={ratio:.0%}")
            if item["confidence"] < THRESHOLDS["inactive_threshold"]:
                item["status"] = "inactive"
                updates["l5_inactive"].append(item["id"])
                print("       -> inactive")
    save_json(PATHS["L5"], l5)

    total_changes = sum(len(v) for v in updates.values())
    if total_changes == 0:
        print("[StrategyScorer] 無足夠樣本或暫無需調整的策略")
        return
    print(f"[StrategyScorer] 完成，共調整 {total_changes} 項")


# ─────────────────────────────────────────────
# --init
# ─────────────────────────────────────────────

def cmd_init():
    print("初始化記憶系統...")
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_ROOT.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    for key, path in PATHS.items():
        if not path.exists():
            if str(path).endswith(".json"):
                save_json(path, [])
            elif str(path).endswith(".jsonl"):
                path.write_text("", encoding="utf-8")
            else:
                path.write_text("", encoding="utf-8")
            print(f"  建立 {path.name}")
        else:
            print(f"  已存在 {path.name}，略過")

    print("初始化完成")


# ─────────────────────────────────────────────
# --record：Episode Importance Scoring → L3
# ─────────────────────────────────────────────

def episode_importance_score(
    task: str,
    status: int,
    diff_summary: str,
    harness_delta: float = 0.0,
) -> tuple[int, list[str], str]:
    """計算事件重要性分數"""

    score = 0
    reasons = []
    episodes = load_jsonl(PATHS["L3"])

    # 是否第一次出現這類問題
    task_keywords = set(task.lower().split()[:5])
    similar = [
        e for e in episodes
        if set(e.get("task", "").lower().split()[:5]) & task_keywords
    ]
    if len(similar) == 0:
        score += 3
        reasons.append("第一次出現這類問題")

    # 是否導致 rollback
    if status == 2:
        score += 5
        reasons.append("導致 rollback")

    # 是否大幅提升 Harness 分數
    if harness_delta >= 1.5:
        score += 4
        reasons.append(f"Harness 分數提升 {harness_delta:.1f}")

    # 是否跨多任務可重用
    keywords_in_diff = any(
        kw in diff_summary.lower()
        for kw in ["function", "class", "module", "util", "helper", "config", "schema"]
    )
    if keywords_in_diff:
        score += 3
        reasons.append("可能跨任務重用")

    # 失敗事件額外加分（錯誤更有學習價值）
    if status != 0:
        score += 2
        reasons.append("失敗事件（學習價值高）")

    priority = "high" if score >= 6 else ("normal" if score >= 3 else "low")
    pool = "validated" if score >= THRESHOLDS["episode_min_score"] else "candidate"

    return score, reasons, priority, pool


def build_training_sample(record: dict) -> Optional[dict]:
    artifacts = record.get("artifacts", {})
    prompt_excerpt = artifacts.get("prompt_excerpt", "").strip()
    strategy_summary = artifacts.get("patch_summary", "").strip() or record.get("diff_summary", "").strip()
    if not prompt_excerpt or not strategy_summary:
        return None

    context = record.get("context", {})
    workflow = record.get("workflow", {})
    files = context.get("files", [])
    l4_ids = context.get("selected_l4_ids", [])
    l5_ids = context.get("selected_l5_ids", [])
    harness_excerpt = artifacts.get("harness_excerpt", "").strip()

    user_parts = [
        f"Task:\n{record.get('task', '')}",
        f"Prompt excerpt:\n{prompt_excerpt}",
    ]
    if files:
        user_parts.append(f"Candidate files:\n{', '.join(files)}")
    if l4_ids:
        user_parts.append(f"Selected L4:\n{', '.join(l4_ids)}")
    if l5_ids:
        user_parts.append(f"Selected L5:\n{', '.join(l5_ids)}")
    if workflow:
        workflow_lines = [
            f"task_type={workflow.get('task_type', '')}",
            f"complexity={workflow.get('complexity', '')}",
            f"mode={workflow.get('execution_mode', '')}",
        ]
        if workflow.get("goal"):
            workflow_lines.append(f"goal={workflow.get('goal')}")
        if workflow.get("constraints"):
            workflow_lines.append(f"constraints={workflow.get('constraints')}")
        if workflow.get("acceptance"):
            workflow_lines.append(f"acceptance={workflow.get('acceptance')}")
        if workflow.get("file_plan"):
            workflow_lines.append(f"file_plan={workflow.get('file_plan')}")
        if workflow.get("research_reason"):
            workflow_lines.append(f"research_reason={workflow.get('research_reason')}")
        if workflow.get("rollback_reason"):
            workflow_lines.append(f"rollback_reason={workflow.get('rollback_reason')}")
        user_parts.append(f"Workflow signals:\n" + "\n".join(workflow_lines))
    if harness_excerpt and record.get("status") != 0:
        user_parts.append(f"Recent harness failure:\n{harness_excerpt}")

    expected_outcome = "讓 harness 通過並完成任務" if record.get("status") == 0 else "先縮小風險並避免重複失敗"
    assistant_parts = [
        f"策略摘要：{strategy_summary}",
        f"建議修改檔案：{', '.join(files) if files else '(未提供)'}",
        f"預期結果：{expected_outcome}",
    ]

    return {
        "id": record.get("id"),
        "messages": [
            {
                "role": "system",
                "content": "你是程式任務策略模型。根據任務、記憶注入與最近失敗訊號，輸出下一步修復策略摘要。",
            },
            {
                "role": "user",
                "content": "\n\n".join(user_parts),
            },
            {
                "role": "assistant",
                "content": "\n".join(assistant_parts),
            },
        ],
        "metadata": {
            "status": record.get("status"),
            "score": record.get("score"),
            "pool": record.get("pool"),
            "task_complete": record.get("task_complete", False),
            "workflow_mode": workflow.get("execution_mode", ""),
            "task_type": workflow.get("task_type", ""),
        },
    }


def cmd_record(
    status: int,
    task: str,
    diff_summary: str,
    harness_delta: float = 0.0,
    *,
    files: Optional[list[str]] = None,
    iteration: int = 0,
    max_iter: int = 0,
    aider_model: str = "",
    harness_cmd: str = "",
    work_dir: str = "",
    prompt_excerpt: str = "",
    harness_excerpt: str = "",
    selected_l4_ids: Optional[list[str]] = None,
    selected_l5_ids: Optional[list[str]] = None,
    patch_summary: str = "",
    task_complete: bool = False,
    run_id: str = "",
    trace_dir: str = "",
    failure_mode: str = "",
    root_cause: str = "",
    patch_type: str = "",
    repo_type: str = "",
    language: str = "",
    framework: str = "",
    test_scope: str = "",
    workflow_task_type: str = "",
    workflow_complexity: str = "",
    workflow_mode: str = "",
    workflow_file_plan: str = "",
    workflow_goal: str = "",
    workflow_constraints: str = "",
    workflow_acceptance: str = "",
    workflow_research_reason: str = "",
    workflow_rollback_reason: str = "",
    workflow_strategy_note: str = "",
):
    score, reasons, priority, pool = episode_importance_score(
        task, status, diff_summary, harness_delta
    )

    files = files or []
    selected_l4_ids = selected_l4_ids or []
    selected_l5_ids = selected_l5_ids or []
    framework = framework or infer_framework(files, task, harness_cmd, work_dir)
    repo_type = repo_type or infer_repo_type(framework, files, task)
    language = language or infer_language(files)
    test_scope = test_scope or infer_test_scope(harness_cmd)
    failure_mode, root_cause = (
        (failure_mode, root_cause)
        if failure_mode and root_cause
        else infer_failure_taxonomy(status, harness_excerpt, diff_summary)
    )
    patch_type = patch_type or infer_patch_type(diff_summary, files, task)

    ep_id = gen_id("ep", PATHS["L3"])
    record = {
        "id": ep_id,
        "time": datetime.now().isoformat(),
        "task": task[:200],
        "status": status,
        "diff_summary": diff_summary[:300],
        "harness_delta": harness_delta,
        "score": score,
        "reasons": reasons,
        "priority": priority,
        "pool": pool,
        "task_complete": task_complete,
        "run_id": run_id,
        "trace_dir": trace_dir,
        "context": {
            "iteration": iteration,
            "max_iter": max_iter,
            "aider_model": aider_model[:120],
            "harness_cmd": sanitize_text(harness_cmd, 300),
            "work_dir": work_dir[:300],
            "files": files[:20],
            "selected_l4_ids": selected_l4_ids[:10],
            "selected_l5_ids": selected_l5_ids[:10],
        },
        "artifacts": {
            "prompt_excerpt": sanitize_text(prompt_excerpt, 1500),
            "harness_excerpt": sanitize_text(harness_excerpt, 1200),
            "patch_summary": sanitize_text(patch_summary, 1200),
        },
        "taxonomy": {
            "failure_mode": failure_mode,
            "root_cause": root_cause,
            "patch_type": patch_type,
            "repo_type": repo_type,
            "language": language,
            "framework": framework,
            "test_scope": test_scope,
        },
        "workflow": {
            "task_type": workflow_task_type,
            "complexity": workflow_complexity,
            "execution_mode": workflow_mode,
            "goal": sanitize_text(workflow_goal, 400),
            "constraints": sanitize_text(workflow_constraints, 800),
            "acceptance": sanitize_text(workflow_acceptance, 800),
            "file_plan": sanitize_text(workflow_file_plan, 1200),
            "research_reason": workflow_research_reason,
            "rollback_reason": workflow_rollback_reason,
            "strategy_note": sanitize_text(workflow_strategy_note, 1200),
        },
    }
    record["training_ready"] = build_training_sample(record) is not None

    append_jsonl(PATHS["L3"], record)
    update_strategy_effectiveness(record)
    print(f"[L3] 記錄事件 {ep_id} | 分數：{score} | 優先：{priority} | 池：{pool}")
    if reasons:
        for r in reasons:
            print(f"       + {r}")


def cmd_export_training_data(output_path: str = "", include_failed: bool = False) -> None:
    episodes = load_jsonl(PATHS["L3"])
    samples = []
    for record in episodes:
        if not include_failed and record.get("status") != 0:
            continue
        sample = build_training_sample(record)
        if sample:
            samples.append(sample)

    if not samples:
        print("[Export] 沒有可匯出的訓練樣本")
        return

    if output_path:
        target = Path(output_path)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = EXPORT_DIR / f"training_data_{timestamp}.jsonl"

    write_jsonl(target, samples)
    print(f"[Export] 已輸出 {len(samples)} 筆訓練樣本 → {target}")


# ─────────────────────────────────────────────
# --update-mempalace：MemPalace 四區壓縮 → CLAUDE.md
# ─────────────────────────────────────────────

def cmd_update_mempalace(context: str = ""):
    if not context:
        context = read_md(PATHS["L2"])
        if not context:
            print("[MemPalace] L2 工作記憶為空，略過")
            return

    prompt = f"""你是 context 壓縮系統。
輸入：當前工作 context
{context}

輸出嚴格四區格式，禁止單純摘要：

[GOAL]
當前任務目標（原始，不可壓縮）

[DONE]
已完成且驗證通過的事項（條列）

[PENDING]
未完成 / 驗證中 / 待確認事項（條列）

[CONSTRAINTS]
不可遺失的約束條件、設計決策、tradeoff（條列）

規則：
- 禁止把四區合併成一段摘要
- 禁止省略 PENDING 項目
- 禁止省略 CONSTRAINTS
- 每個區塊至少一條內容
- 使用繁體中文
"""

    if not budget_check(prompt, "--update-mempalace (governor)"):
        print("[MemPalace] token budget 超出，略過壓縮")
        return

    result = ollama_call(prompt, model=OLLAMA_MODEL_GOVERNOR)

    # ── 唯一真相：先寫 L2 ──────────────────────────────
    write_md(PATHS["L2"], result)
    print("[MemPalace] L2_working.md 已更新（唯一真相）")

    # ── 展示層：從 L2 mirror 到 CLAUDE.md ─────────────
    claude_content = read_md(CLAUDE_MD)
    if claude_content:
        updated = replace_block(
            claude_content,
            "<!-- MEMPALACE_START -->",
            "<!-- MEMPALACE_END -->",
            result,
        )
        write_md(CLAUDE_MD, updated)
        print("[MemPalace] CLAUDE.md 已同步（展示層 mirror）")
    else:
        print("[WARN] CLAUDE.md 不存在，略過 mirror")


# ─────────────────────────────────────────────
# --sleep：L3 驗證池 → L4 知識萃取
# ─────────────────────────────────────────────

def group_similar_episodes(episodes: list) -> dict[str, list]:
    """簡單關鍵字分群，同類事件歸在一起"""
    groups: dict[str, list] = {}
    for ep in episodes:
        task_words = frozenset(ep.get("task", "").lower().split()[:8])
        matched = None
        for key in groups:
            key_words = frozenset(key.split())
            overlap = len(task_words & key_words) / max(len(task_words | key_words), 1)
            if overlap >= 0.3:
                matched = key
                break
        if matched:
            groups[matched].append(ep)
        else:
            groups[" ".join(list(task_words)[:5])].append(ep)
    return groups


def cmd_sleep():
    print("[Sleep] 開始淺眠整合（L3 → L4）...")

    all_episodes = load_jsonl(PATHS["L3"])
    high_priority = [
        e for e in all_episodes
        if e.get("pool") == "validated" and e.get("score", 0) >= THRESHOLDS["episode_min_score"]
    ]

    if not high_priority:
        print("[Sleep] 沒有足夠的高分事件，略過")
        return

    groups = group_similar_episodes(high_priority)
    qualified_groups = {
        k: v for k, v in groups.items()
        if len(v) >= THRESHOLDS["l3_to_l4_min_count"]
    }

    if not qualified_groups:
        print(f"[Sleep] 無同類事件達到 {THRESHOLDS['l3_to_l4_min_count']} 次門檻")
        print(f"        目前群組數量：{len(groups)}，各群數量：{[len(v) for v in groups.values()]}")
        return

    # ── Cost Governor：群組數截斷 ──────────────────────
    if len(qualified_groups) > MAX_SLEEP_GROUPS:
        # 優先處理成員數最多的群組（代表性最強）
        qualified_groups = dict(
            sorted(qualified_groups.items(), key=lambda x: -len(x[1]))[:MAX_SLEEP_GROUPS]
        )
        print(f"[Sleep] 群組數截斷至 {MAX_SLEEP_GROUPS}（Cost Governor）")

    l4_current = load_json(PATHS["L4"])

    episodes_text = json.dumps(
        [{"id": e["id"], "task": e["task"], "diff": e.get("diff_summary", ""), "score": e["score"]}
         for group in qualified_groups.values() for e in group],
        ensure_ascii=False, indent=2
    )

    l4_text = json.dumps(
        [{"id": k["id"], "pattern": k["pattern"], "scope": k["scope"]}
         for k in l4_current if k.get("status") == "active"],
        ensure_ascii=False, indent=2
    )

    prompt = f"""你是知識萃取系統。

輸入：今日高分 L3 事件（同類已出現 >= {THRESHOLDS['l3_to_l4_min_count']} 次）
{episodes_text}

現有 L4 知識庫（避免重複）：
{l4_text}

任務：
1. 從事件中萃取可泛化規律
2. 與現有 L4 比對，完全相同的規律不重複新增
3. 計算初始 confidence（首次不得超過 0.75）

輸出嚴格 JSON 陣列（只輸出新增的知識）：
[
  {{
    "pattern": "規律描述（50字內，繁體中文）",
    "scope": "適用範圍（越具體越好，禁止寫通用或所有情況）",
    "evidence_count": 3,
    "confidence": 0.65,
    "source_episodes": ["ep_id清單"]
  }}
]

禁止：
- pattern 包含執行步驟（那是L5的事）
- confidence 首次超過 0.75
- scope 寫「通用」或「所有情況」
- 輸出 JSON 以外的任何文字
"""

    # ── Phase 1：Researcher（Gemma）產候選 L4 ───────────
    if not budget_check(prompt, "--sleep phase1 researcher"):
        print("[Sleep] token budget 超出，略過此批次")
        return

    raw_candidates = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER)
    candidates = parse_json_from_response(raw_candidates)

    if not candidates or not isinstance(candidates, list):
        print("[Sleep] Researcher 未回傳有效候選 L4")
        return

    print(f"[Sleep] Phase 1 完成，{len(candidates)} 條候選 → 送 Governor 複核")

    # ── Phase 2：Governor（Qwen）複核 ────────────────────
    review_prompt = f"""你是知識品質審核員。逐條審核以下候選 L4 知識。

候選 L4：
{json.dumps(candidates, ensure_ascii=False, indent=2)}

現有 L4 知識庫（避免重複）：
{l4_text}

審核規則（每條獨立判斷）：
1. 與現有 L4 實質重複 → 刪除
2. pattern 混入了執行步驟（how-to）→ 修正為純觀察，或刪除
3. confidence 超過 0.75 → 調降至 0.70
4. scope 過於泛化（「通用」「所有情況」）→ 刪除

輸出：通過審核的條目，格式與輸入相同，僅輸出 JSON 陣列，不含任何說明。
"""

    if not budget_check(review_prompt, "--sleep phase2 governor"):
        print("[Sleep] Governor 複核 budget 超出，沿用 Phase 1 結果（confidence 安全調降）")
        # 安全降級：confidence 壓低，避免未審核知識過度自信
        new_items = [
            {**item, "confidence": min(float(item.get("confidence", 0.65)), 0.60)}
            for item in candidates
        ]
    else:
        raw_reviewed = ollama_call(review_prompt, model=OLLAMA_MODEL_GOVERNOR)
        reviewed = parse_json_from_response(raw_reviewed)
        if not reviewed or not isinstance(reviewed, list):
            print("[Sleep] Governor 未回傳有效審核結果，沿用 Phase 1 結果")
            new_items = candidates
        else:
            new_items = reviewed
            print(f"[Sleep] Phase 2 完成：{len(candidates)} 候選 → {len(new_items)} 通過複核")

    now = datetime.now().strftime("%Y-%m-%d")
    added = 0
    for item in new_items:
        if not item.get("pattern"):
            continue
        record = {
            "id": f"k_{len(l4_current) + added + 1:03d}",
            "pattern": item.get("pattern", ""),
            "scope": item.get("scope", "未指定"),
            "evidence_count": item.get("evidence_count", len(high_priority)),
            "confidence": min(float(item.get("confidence", 0.65)), 0.75),
            "source_episodes": item.get("source_episodes", []),
            "last_verified": now,
            "decay_timer": THRESHOLDS["decay_days"],
            "status": "active",
        }
        l4_current.append(record)
        added += 1
        print(f"  [L4+] {record['id']} | {record['pattern'][:40]} | conf={record['confidence']}")

    save_json(PATHS["L4"], l4_current)
    print(f"[Sleep] 完成，新增 {added} 條 L4 知識")


# ─────────────────────────────────────────────
# Conflict Resolver
# ─────────────────────────────────────────────

def condition_overlap(cond_a: str, cond_b: str) -> float:
    words_a = set(cond_a.lower().split())
    words_b = set(cond_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def resolve_conflict(existing: dict, challenger: dict) -> tuple[dict, dict, str]:
    """
    比較兩條 L5 策略，回傳 (winner, loser, reason)
    優先順序：scope 小 > last_verified 近 > success_count > confidence
    """
    def scope_specificity(s: dict) -> int:
        scope = s.get("scope", "")
        for val, score in [("日線", 4), ("週線", 3), ("台股", 2), ("股票", 1)]:
            if val in scope:
                return score
        return 0

    scores = {"existing": 0, "challenger": 0}

    if scope_specificity(existing) > scope_specificity(challenger):
        scores["existing"] += 3
    elif scope_specificity(challenger) > scope_specificity(existing):
        scores["challenger"] += 3

    try:
        d_ex = datetime.fromisoformat(existing.get("last_verified", "2000-01-01"))
        d_ch = datetime.fromisoformat(challenger.get("last_verified", "2000-01-01"))
        if d_ex > d_ch:
            scores["existing"] += 2
        elif d_ch > d_ex:
            scores["challenger"] += 2
    except ValueError:
        pass

    if existing.get("success_count", 0) > challenger.get("success_count", 0):
        scores["existing"] += 1
    elif challenger.get("success_count", 0) > existing.get("success_count", 0):
        scores["challenger"] += 1

    if existing.get("confidence", 0) > challenger.get("confidence", 0):
        scores["existing"] += 1
    elif challenger.get("confidence", 0) > existing.get("confidence", 0):
        scores["challenger"] += 1

    if scores["existing"] == scores["challenger"]:
        return existing, challenger, "needs_review"
    elif scores["existing"] > scores["challenger"]:
        reason = f"現有策略勝出（scope/驗證/成功率）"
        return existing, challenger, reason
    else:
        reason = f"新策略勝出（scope/驗證/成功率）"
        return challenger, existing, reason


# ─────────────────────────────────────────────
# --deep：L4 → L5 策略固化
# ─────────────────────────────────────────────

def cmd_deep():
    print("[Deep] 開始深眠固化（L4 → L5）...")

    l4_all = load_json(PATHS["L4"])
    qualified = [
        k for k in l4_all
        if k.get("status") == "active"
        and float(k.get("confidence", 0)) >= THRESHOLDS["l4_to_l5_min_conf"]
    ]

    if not qualified:
        print(f"[Deep] 無 L4 知識達到 confidence >= {THRESHOLDS['l4_to_l5_min_conf']}")
        return

    # ── Cost Governor：L4 條數截斷 ─────────────────────
    if len(qualified) > MAX_DEEP_EPISODES:
        qualified = sorted(qualified, key=lambda x: x.get("confidence", 0), reverse=True)
        qualified = qualified[:MAX_DEEP_EPISODES]
        print(f"[Deep] L4 截斷至 {MAX_DEEP_EPISODES} 條（取 confidence 最高，Cost Governor）")

    l5_current = load_json(PATHS["L5"])

    l4_text = json.dumps(
        [{"id": k["id"], "pattern": k["pattern"], "scope": k["scope"], "confidence": k["confidence"]}
         for k in qualified],
        ensure_ascii=False, indent=2
    )

    l5_text = json.dumps(
        [{"id": s["id"], "condition": s["condition"], "scope": s["scope"]}
         for s in l5_current if s.get("status") == "active"],
        ensure_ascii=False, indent=2
    )

    prompt = f"""你是策略固化系統。

輸入：confidence >= {THRESHOLDS['l4_to_l5_min_conf']} 的 L4 知識
{l4_text}

現有 L5 策略庫：
{l5_text}

任務：
1. 將每條知識轉化為可執行策略
2. 偵測 condition 與現有 L5 是否重疊（重疊 > 50% 視為衝突）
3. 標記衝突狀態

輸出嚴格 JSON 陣列（只輸出新策略）：
[
  {{
    "condition": "當[具體條件]時（禁止模糊描述）",
    "action": "執行[具體可操作步驟]",
    "avoid": "不要[具體禁忌]",
    "scope": "適用範圍（與來源 L4 一致）",
    "source_knowledge": "來源 L4 的 id",
    "confidence": 0.80,
    "conflict_check": "無衝突（如有衝突請寫：與 s_xxx 衝突）"
  }}
]

禁止：
- condition 包含觀察描述（那是L4的事）
- 輸出 JSON 以外的任何文字
"""

    # ── Phase 1：Researcher（Gemma）產 L5 草稿 ──────────
    if not budget_check(prompt, "--deep phase1 researcher"):
        print("[Deep] token budget 超出，略過此批次")
        return

    raw_drafts = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER)
    drafts = parse_json_from_response(raw_drafts)

    if not drafts or not isinstance(drafts, list):
        print("[Deep] Researcher 未回傳有效 L5 草稿")
        return

    print(f"[Deep] Phase 1 完成，{len(drafts)} 條草稿 → 送 Governor 審核")

    # ── Phase 2：Governor（Qwen）審核草稿 ────────────────
    gov_review_prompt = f"""你是策略品質審核員。審核以下 L5 策略草稿。

草稿策略：
{json.dumps(drafts, ensure_ascii=False, indent=2)}

現有 L5 策略庫：
{l5_text}

審核規則（每條獨立判斷）：
1. condition 包含觀察描述而非執行條件 → 修正為「當...時」格式
2. action 不夠具體可操作 → 修正
3. confidence 超過 0.85（首次策略不得超過） → 調降至 0.80
4. scope 與現有策略完全重疊 → 標記 conflict_check 為 "needs_review"
5. 格式不完整（缺 condition / action / avoid）→ 刪除

輸出：審核後的策略陣列，格式與輸入相同，僅輸出 JSON 陣列，不含說明。
"""

    if not budget_check(gov_review_prompt, "--deep phase2 governor"):
        print("[Deep] Governor 審核 budget 超出，沿用 Phase 1 草稿（confidence 安全調降）")
        new_items = [
            {**d, "confidence": min(float(d.get("confidence", 0.80)), 0.75)}
            for d in drafts
        ]
    else:
        raw_reviewed = ollama_call(gov_review_prompt, model=OLLAMA_MODEL_GOVERNOR)
        reviewed = parse_json_from_response(raw_reviewed)
        if not reviewed or not isinstance(reviewed, list):
            print("[Deep] Governor 未回傳有效審核結果，沿用 Phase 1 草稿")
            new_items = drafts
        else:
            new_items = reviewed
            print(f"[Deep] Phase 2 完成：{len(drafts)} 草稿 → {len(new_items)} 通過審核")

    now = datetime.now().strftime("%Y-%m-%d")
    added = 0
    conflict_count = 0

    for item in new_items:
        if not item.get("condition"):
            continue

        conflict_result = "無衝突"
        conflict_with = None

        for existing in l5_current:
            if existing.get("status") != "active":
                continue
            overlap = condition_overlap(
                item.get("condition", ""),
                existing.get("condition", "")
            )
            if overlap >= THRESHOLDS["conflict_overlap"]:
                winner, loser, reason = resolve_conflict(existing, item)
                if reason == "needs_review":
                    conflict_result = "needs_review"
                    conflict_with = existing["id"]
                elif winner["id"] == existing["id"]:
                    conflict_result = f"衝突：現有 {existing['id']} 勝出，新策略不加入"
                    conflict_with = existing["id"]
                    print(f"  [衝突] 新策略 vs {existing['id']} → 現有策略保留")
                    conflict_count += 1
                    break
                else:
                    existing["status"] = "inactive"
                    existing["conflict_note"] = f"被新策略取代：{reason}"
                    conflict_result = f"衝突已解決：{existing['id']} 降為 inactive"
                    conflict_with = existing["id"]
                    print(f"  [衝突解決] {existing['id']} → inactive，新策略加入")
                    conflict_count += 1

        if "勝出，新策略不加入" in conflict_result:
            continue

        record = {
            "id": f"s_{len(l5_current) + added + 1:03d}",
            "condition": item.get("condition", ""),
            "action": item.get("action", ""),
            "avoid": item.get("avoid", ""),
            "scope": item.get("scope", "未指定"),
            "source_knowledge": item.get("source_knowledge", ""),
            "confidence": float(item.get("confidence", 0.80)),
            "success_count": 0,
            "fail_count": 0,
            "last_verified": now,
            "decay_timer": THRESHOLDS["decay_days"],
            "status": "active" if conflict_result != "needs_review" else "active",
            "conflict_check": conflict_result,
            "conflict_with": conflict_with,
        }

        l5_current.append(record)
        added += 1
        flag = "⚠️ needs_review" if conflict_result == "needs_review" else "✓"
        print(f"  [L5+] {record['id']} {flag} | {record['condition'][:40]}")

    save_json(PATHS["L5"], l5_current)
    print(f"[Deep] 完成，新增 {added} 條 L5 策略，衝突處理 {conflict_count} 件")


# ─────────────────────────────────────────────
# --decay：Memory Decay 衰減檢查
# ─────────────────────────────────────────────

def cmd_decay():
    print("[Decay] 執行記憶衰減檢查...")
    now = datetime.now()
    decay_report = {"l4_decayed": [], "l4_inactive": [], "l5_decayed": [], "l5_downgraded": [], "l5_retired": []}

    # L4 衰減
    l4 = load_json(PATHS["L4"])
    for item in l4:
        if item.get("status") in ("inactive", "retired"):
            last = datetime.fromisoformat(item.get("last_verified", "2000-01-01"))
            if item.get("status") == "inactive":
                days_inactive = (now - last).days
                if days_inactive >= THRESHOLDS["retire_days"]:
                    item["status"] = "retired"
                    decay_report["l5_retired"].append(item["id"])
                    print(f"  [L4] {item['id']} → retired（{days_inactive}天未驗證）")
            continue

        try:
            last_verified = datetime.fromisoformat(item.get("last_verified", str(now.date())))
        except ValueError:
            continue

        days_since = (now - last_verified).days
        item["decay_timer"] = max(0, THRESHOLDS["decay_days"] - days_since)

        if days_since >= THRESHOLDS["decay_days"]:
            weeks_over = (days_since - THRESHOLDS["decay_days"]) // 7 + 1
            old_conf = item.get("confidence", 0.7)
            item["confidence"] = max(0.0, round(old_conf - THRESHOLDS["decay_amount"] * weeks_over, 3))
            decay_report["l4_decayed"].append(item["id"])
            print(f"  [L4] {item['id']} confidence {old_conf:.2f} → {item['confidence']:.2f}")

        if item.get("confidence", 1.0) < THRESHOLDS["inactive_threshold"]:
            item["status"] = "inactive"
            decay_report["l4_inactive"].append(item["id"])
            print(f"  [L4] {item['id']} → inactive（confidence 過低）")

    save_json(PATHS["L4"], l4)

    # L5 衰減
    l5 = load_json(PATHS["L5"])
    for item in l5:
        if item.get("status") == "retired":
            continue

        if item.get("status") == "inactive":
            try:
                last = datetime.fromisoformat(item.get("last_verified", "2000-01-01"))
                if (now - last).days >= THRESHOLDS["retire_days"]:
                    item["status"] = "retired"
                    decay_report["l5_retired"].append(item["id"])
                    print(f"  [L5] {item['id']} → retired")
            except ValueError:
                pass
            continue

        try:
            last_verified = datetime.fromisoformat(item.get("last_verified", str(now.date())))
        except ValueError:
            continue

        days_since = (now - last_verified).days
        item["decay_timer"] = max(0, THRESHOLDS["decay_days"] - days_since)

        if days_since >= THRESHOLDS["decay_days"]:
            weeks_over = (days_since - THRESHOLDS["decay_days"]) // 7 + 1
            old_conf = item.get("confidence", 0.8)
            item["confidence"] = max(0.0, round(old_conf - THRESHOLDS["decay_amount"] * weeks_over, 3))
            decay_report["l5_decayed"].append(item["id"])
            print(f"  [L5] {item['id']} confidence {old_conf:.2f} → {item['confidence']:.2f}")

        # fail ratio 過高 → 降級回 L4 候選
        total = item.get("success_count", 0) + item.get("fail_count", 0)
        if total >= 3:
            fail_ratio = item.get("fail_count", 0) / total
            if fail_ratio >= THRESHOLDS["fail_ratio_threshold"]:
                item["status"] = "inactive"
                item["downgrade_reason"] = f"fail_ratio={fail_ratio:.2f} 超過門檻"
                decay_report["l5_downgraded"].append(item["id"])
                print(f"  [L5] {item['id']} → inactive（失敗率 {fail_ratio:.0%}）")

        if item.get("confidence", 1.0) < THRESHOLDS["inactive_threshold"]:
            item["status"] = "inactive"
            print(f"  [L5] {item['id']} → inactive（confidence 過低）")

    save_json(PATHS["L5"], l5)

    total_changes = sum(len(v) for v in decay_report.values())
    if total_changes == 0:
        print("[Decay] 無需衰減，所有記憶狀態正常")
    else:
        print(f"[Decay] 完成，共處理 {total_changes} 項變更")


# ─────────────────────────────────────────────
# --wake：注入 L4+L5 → CLAUDE.md + PROMPT.md
# ─────────────────────────────────────────────

def cmd_wake():
    print("[Wake] 開始醒來注入...")

    # 先執行衰減
    cmd_decay()

    now = datetime.now()
    l4_all = load_json(PATHS["L4"])
    l5_all = load_json(PATHS["L5"])

    l4_inject = [
        k for k in l4_all
        if k.get("status") == "active"
        and float(k.get("confidence", 0)) >= THRESHOLDS["wake_l4_min_conf"]
    ]
    l4_inject.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    l5_inject = [
        s for s in l5_all
        if s.get("status") == "active"
        and float(s.get("confidence", 0)) >= THRESHOLDS["wake_l5_min_conf"]
        and s.get("conflict_check") != "needs_review"
    ]
    l5_inject.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # 找出需要警告的項目
    warnings = []
    for item in l4_all + l5_all:
        if item.get("status") == "active" and item.get("decay_timer", 99) < THRESHOLDS["decay_warning_days"]:
            warnings.append(f"⚠️ {item['id']} decay_timer={item['decay_timer']}天，今日需驗證")
    for s in l5_all:
        if s.get("conflict_check") == "needs_review":
            warnings.append(f"🔍 {s['id']} 策略衝突未解決，需人工確認")

    # 組合注入內容
    l4_lines = "\n".join(
        f"- [{k['id']} conf={k['confidence']:.2f}] {k['pattern']} （{k['scope']}）"
        for k in l4_inject[:5]
    )

    l5_lines = "\n".join(
        f"- 當 {s['condition']}：{s['action']}（禁止：{s['avoid']}）"
        for s in l5_inject[:5]
    )

    warn_lines = "\n".join(warnings[:5]) if warnings else "（無）"

    inject_block = f"""## 今日應記住的規律（L4）
{l4_lines if l4_lines else '（尚無足夠知識）'}

## 今日執行策略（L5）
{l5_lines if l5_lines else '（尚無足夠策略）'}

## 今日特別注意
{warn_lines}

## 注入時間
{now.strftime('%Y-%m-%d %H:%M')}
"""

    # 更新 CLAUDE.md
    claude_content = read_md(CLAUDE_MD)
    if claude_content:
        updated = replace_block(claude_content, "<!-- L4_INJECT_START -->", "<!-- L4_INJECT_END -->",
                                 l4_lines or "（尚無）")
        updated = replace_block(updated, "<!-- L5_INJECT_START -->", "<!-- L5_INJECT_END -->",
                                 l5_lines or "（尚無）")
        write_md(CLAUDE_MD, updated)
        print("[Wake] CLAUDE.md L4/L5 區塊已更新")

    # 更新 PROMPT.md
    prompt_content = read_md(PROMPT_MD)
    if prompt_content:
        updated = replace_block(prompt_content, "<!-- L4_INJECT_START -->", "<!-- L4_INJECT_END -->",
                                 l4_lines or "（尚無）")
        updated = replace_block(updated, "<!-- L5_INJECT_START -->", "<!-- L5_INJECT_END -->",
                                 l5_lines or "（尚無）")
        write_md(PROMPT_MD, updated)
        print("[Wake] PROMPT.md L4/L5 區塊已更新")

    print(f"[Wake] 注入 L4 知識：{len(l4_inject)} 條，L5 策略：{len(l5_inject)} 條")
    if warnings:
        print(f"[Wake] 警告項目：{len(warnings)} 件")
        for w in warnings:
            print(f"       {w}")

    append_jsonl(PATHS["WAKE_HISTORY"], {
        "time": now.isoformat(),
        "injected_l4_ids": [item["id"] for item in l4_inject[:5]],
        "injected_l5_ids": [item["id"] for item in l5_inject[:5]],
        "active_l4_count": len([item for item in l4_all if item.get("status") == "active"]),
        "active_l5_count": len([item for item in l5_all if item.get("status") == "active"]),
        "warning_count": len(warnings),
        "warnings": warnings[:5],
    })


# ─────────────────────────────────────────────
# Bridge Schema Validator（純 Python，不呼叫模型）
# ─────────────────────────────────────────────

def bridge_schema_validate(result: dict) -> tuple[bool, list[str]]:
    """
    驗證 Bridge 輸出的 JSON schema。
    回傳 (is_valid, error_list)。
    設計原則：模型負責生成，程式負責格式把關，問題在進 L3 前攔截。
    """
    errors: list[str] = []

    # L3_events：字串 list，每條 <= 100 字
    l3 = result.get("L3_events")
    if l3 is None:
        errors.append("缺少 L3_events 欄位")
    elif not isinstance(l3, list):
        errors.append(f"L3_events 必須是 list，得到 {type(l3).__name__}")
    else:
        for i, e in enumerate(l3):
            if not isinstance(e, str):
                errors.append(f"L3_events[{i}] 必須是字串，得到 {type(e).__name__}")
            elif len(e) > 100:
                errors.append(f"L3_events[{i}] 超過 100 字（{len(e)} 字）")

    # L4_candidates：字串 list，每條 <= 150 字
    l4 = result.get("L4_candidates")
    if l4 is None:
        errors.append("缺少 L4_candidates 欄位")
    elif not isinstance(l4, list):
        errors.append(f"L4_candidates 必須是 list，得到 {type(l4).__name__}")
    else:
        for i, c in enumerate(l4):
            if not isinstance(c, str):
                errors.append(f"L4_candidates[{i}] 必須是字串")
            elif len(c) > 150:
                errors.append(f"L4_candidates[{i}] 超過 150 字（{len(c)} 字）")

    # L5_candidates：dict list，必填欄位 condition / action / avoid / confidence
    l5 = result.get("L5_candidates")
    if l5 is None:
        errors.append("缺少 L5_candidates 欄位")
    elif not isinstance(l5, list):
        errors.append(f"L5_candidates 必須是 list，得到 {type(l5).__name__}")
    else:
        required = {"condition", "action", "avoid", "confidence"}
        for i, s in enumerate(l5):
            if not isinstance(s, dict):
                errors.append(f"L5_candidates[{i}] 必須是 dict")
                continue
            missing = required - s.keys()
            if missing:
                errors.append(f"L5_candidates[{i}] 缺少欄位：{missing}")
            if "confidence" in s:
                try:
                    conf = float(s["confidence"])
                    if not 0.0 <= conf <= 1.0:
                        errors.append(
                            f"L5_candidates[{i}].confidence={conf} 超出 [0,1]"
                        )
                except (TypeError, ValueError):
                    errors.append(f"L5_candidates[{i}].confidence 不是數字")
            for field in ("condition", "action", "avoid"):
                if field in s and not isinstance(s[field], str):
                    errors.append(f"L5_candidates[{i}].{field} 必須是字串")

    return len(errors) == 0, errors


# ─────────────────────────────────────────────
# --bridge：MemPalace 輸出 → L3 格式
# ─────────────────────────────────────────────

def cmd_bridge(mempalace_output: str = ""):
    if not mempalace_output:
        mempalace_output = read_md(PATHS["L2"])
    if not mempalace_output:
        print("[Bridge] 無 MemPalace 輸出可處理")
        return

    prompt = f"""你是記憶橋接器。

輸入：今日工作結束後的壓縮對話記錄
{mempalace_output}

從這段記錄中萃取，輸出嚴格 JSON：
{{
  "L3_events": [
    "事件描述（20字內，繁體中文）"
  ],
  "L4_candidates": [
    "可泛化規律（50字內，繁體中文，禁止包含執行步驟）"
  ],
  "L5_candidates": [
    {{
      "condition": "當...時",
      "action": "應該...",
      "avoid": "不要...",
      "confidence": 0.6
    }}
  ]
}}

規則：
- 失敗事件比成功更重要，優先萃取
- L4 只寫觀察到的規律，不含步驟
- L5 只寫執行指令，不含觀察
- 禁止輸出 JSON 以外的文字
"""

    # ── Researcher（Gemma）執行 Bridge 轉換 ──────────────
    raw = ollama_call(prompt, model=OLLAMA_MODEL_RESEARCHER)
    result = parse_json_from_response(raw)

    if not result:
        print("[Bridge] Researcher 未回傳有效結果")
        return

    # ── 純 Python Schema 驗證（不額外呼叫模型）───────────
    is_valid, schema_errors = bridge_schema_validate(result)
    if not is_valid:
        print(f"[Bridge] Schema 驗證失敗（{len(schema_errors)} 項錯誤），拒絕寫入 L3：")
        for err in schema_errors:
            print(f"  ✗ {err}")
        # 記錄一筆低分失敗 episode，供後續追蹤
        append_jsonl(PATHS["L3"], {
            "id": gen_id("ep", PATHS["L3"]),
            "time": datetime.now().isoformat(),
            "task": "Bridge schema 驗證失敗",
            "status": 1,
            "diff_summary": f"schema_errors={len(schema_errors)}",
            "harness_delta": 0,
            "score": 2,
            "reasons": ["bridge schema 驗證失敗"],
            "priority": "low",
            "pool": "candidate",
            "source": "bridge_validate_fail",
        })
        return

    print("[Bridge] Schema 驗證通過")
    now = datetime.now().isoformat()
    added = 0

    for event_text in result.get("L3_events", []):
        record = {
            "id": gen_id("ep", PATHS["L3"]),
            "time": now,
            "task": event_text,
            "status": 0,
            "diff_summary": "bridge_import",
            "harness_delta": 0,
            "score": 6,
            "reasons": ["bridge 轉換"],
            "priority": "normal",
            "pool": "validated",
            "source": "bridge",
        }
        append_jsonl(PATHS["L3"], record)
        added += 1

    print(f"[Bridge] 寫入 L3 事件 {added} 條")
    if result.get("L4_candidates"):
        print(f"[Bridge] L4 候選 {len(result['L4_candidates'])} 條（等待 --sleep 升級）")
    if result.get("L5_candidates"):
        print(f"[Bridge] L5 候選 {len(result['L5_candidates'])} 條（等待 --deep 升級）")


# ─────────────────────────────────────────────
# --status：顯示記憶系統狀態
# ─────────────────────────────────────────────

def cmd_status():
    print("\n═══════════════════════════════════")
    print("  記憶系統狀態")
    print("═══════════════════════════════════")

    episodes = load_jsonl(PATHS["L3"])
    validated = [e for e in episodes if e.get("pool") == "validated"]
    candidate = [e for e in episodes if e.get("pool") == "candidate"]
    training_ready = [e for e in episodes if e.get("training_ready")]
    print(f"\n[L3] 事件池")
    print(f"     驗證池 (score>=6)：{len(validated)} 件")
    print(f"     候選池 (score< 6)：{len(candidate)} 件")
    print(f"     可匯出訓練樣本：{len(training_ready)} 件")
    wake_history = load_jsonl(PATHS["WAKE_HISTORY"])
    strategy_history = load_jsonl(PATHS["STRATEGY_HISTORY"])
    benchmark_history = load_jsonl(PATHS["BENCHMARK_HISTORY"])
    print(f"     wake 歷史：{len(wake_history)} 次")
    print(f"     strategy 使用紀錄：{len(strategy_history)} 件")
    print(f"     benchmark 歷史：{len(benchmark_history)} 次")

    l4 = load_json(PATHS["L4"])
    l4_active   = [k for k in l4 if k.get("status") == "active"]
    l4_inactive = [k for k in l4 if k.get("status") == "inactive"]
    l4_retired  = [k for k in l4 if k.get("status") == "retired"]
    print(f"\n[L4] 知識庫")
    print(f"     active   ：{len(l4_active)} 條")
    print(f"     inactive ：{len(l4_inactive)} 條")
    print(f"     retired  ：{len(l4_retired)} 條")
    if l4_active:
        avg_conf = sum(k.get("confidence", 0) for k in l4_active) / len(l4_active)
        print(f"     平均 confidence：{avg_conf:.2f}")
        top_l4 = sorted(l4_active, key=lambda x: x.get("use_count", 0), reverse=True)[:3]
        if any(item.get("use_count", 0) > 0 for item in top_l4):
            print("     常用 L4：")
            for item in top_l4:
                if item.get("use_count", 0) > 0:
                    print(
                        f"       {item['id']} use={item.get('use_count', 0)} "
                        f"pass={item.get('pass_count', 0)} fail={item.get('fail_count', 0)}"
                    )

    l5 = load_json(PATHS["L5"])
    l5_active   = [s for s in l5 if s.get("status") == "active"]
    l5_inactive = [s for s in l5 if s.get("status") == "inactive"]
    l5_retired  = [s for s in l5 if s.get("status") == "retired"]
    l5_review   = [s for s in l5 if s.get("conflict_check") == "needs_review"]
    print(f"\n[L5] 策略庫")
    print(f"     active       ：{len(l5_active)} 條")
    print(f"     inactive     ：{len(l5_inactive)} 條")
    print(f"     retired      ：{len(l5_retired)} 條")
    print(f"     needs_review ：{len(l5_review)} 條")
    top_l5 = sorted(l5_active, key=lambda x: x.get("use_count", 0), reverse=True)[:3]
    if any(item.get("use_count", 0) > 0 for item in top_l5):
        print("     常用 L5：")
        for item in top_l5:
            if item.get("use_count", 0) > 0:
                print(
                    f"       {item['id']} use={item.get('use_count', 0)} "
                    f"success={item.get('success_count', 0)} fail={item.get('fail_count', 0)}"
                )

    now = datetime.now()
    warn_items = []
    for item in l4_active + l5_active:
        if item.get("decay_timer", 99) < THRESHOLDS["decay_warning_days"]:
            warn_items.append(item)
    if warn_items:
        print(f"\n⚠️  即將衰減（decay_timer < {THRESHOLDS['decay_warning_days']}天）")
        for w in warn_items:
            print(f"   {w['id']} | timer={w.get('decay_timer')}天")

    if l5_review:
        print(f"\n🔍 需人工確認衝突策略")
        for s in l5_review:
            print(f"   {s['id']} | {s['condition'][:40]}")

    print("\n═══════════════════════════════════\n")


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def main():
    global OLLAMA_MODEL, OLLAMA_MODEL_GOVERNOR, OLLAMA_MODEL_RESEARCHER  # 必須在函式最頂端宣告

    parser = argparse.ArgumentParser(
        description="dream_cycle.py — 五層記憶系統核心引擎"
    )
    parser.add_argument("--init",             action="store_true", help="初始化記憶目錄與檔案")
    parser.add_argument("--record",           action="store_true", help="記錄 L3 事件")
    parser.add_argument("--update-mempalace", action="store_true", help="更新 MemPalace 四區")
    parser.add_argument("--sleep",            action="store_true", help="L3 → L4 淺眠整合")
    parser.add_argument("--deep",             action="store_true", help="L4 → L5 深眠固化")
    parser.add_argument("--wake",             action="store_true", help="L4+L5 注入醒來")
    parser.add_argument("--decay",            action="store_true", help="執行記憶衰減")
    parser.add_argument("--bridge",           action="store_true", help="MemPalace → L3 橋接")
    parser.add_argument("--status",           action="store_true", help="顯示記憶系統狀態")
    parser.add_argument("--export-training-data", action="store_true", help="匯出 decision-SFT 訓練樣本")
    parser.add_argument("--score-strategies", action="store_true", help="依 effectiveness 調整 L4/L5 confidence")

    # --record 參數
    parser.add_argument("--status-code",   type=int,   default=0,   dest="status_code")
    parser.add_argument("--task",          type=str,   default="",  dest="task")
    parser.add_argument("--diff-summary",  type=str,   default="",  dest="diff_summary")
    parser.add_argument("--harness-delta", type=float, default=0.0, dest="harness_delta")
    parser.add_argument("--files",         type=str,   default="",  dest="files")
    parser.add_argument("--iteration",     type=int,   default=0,   dest="iteration")
    parser.add_argument("--max-iter",      type=int,   default=0,   dest="max_iter")
    parser.add_argument("--aider-model",   type=str,   default="",  dest="aider_model")
    parser.add_argument("--harness-cmd",   type=str,   default="",  dest="harness_cmd")
    parser.add_argument("--work-dir",      type=str,   default="",  dest="work_dir")
    parser.add_argument("--prompt-excerpt", type=str,  default="",  dest="prompt_excerpt")
    parser.add_argument("--harness-excerpt", type=str, default="",  dest="harness_excerpt")
    parser.add_argument("--selected-l4-ids", type=str, default="",  dest="selected_l4_ids")
    parser.add_argument("--selected-l5-ids", type=str, default="",  dest="selected_l5_ids")
    parser.add_argument("--patch-summary", type=str,   default="",  dest="patch_summary")
    parser.add_argument("--task-complete", type=int,   default=0,   dest="task_complete")
    parser.add_argument("--run-id",        type=str,   default="",  dest="run_id")
    parser.add_argument("--trace-dir",     type=str,   default="",  dest="trace_dir")
    parser.add_argument("--failure-mode",  type=str,   default="",  dest="failure_mode")
    parser.add_argument("--root-cause",    type=str,   default="",  dest="root_cause")
    parser.add_argument("--patch-type",    type=str,   default="",  dest="patch_type")
    parser.add_argument("--repo-type",     type=str,   default="",  dest="repo_type")
    parser.add_argument("--language",      type=str,   default="",  dest="language")
    parser.add_argument("--framework",     type=str,   default="",  dest="framework")
    parser.add_argument("--test-scope",    type=str,   default="",  dest="test_scope")
    parser.add_argument("--workflow-task-type", type=str, default="", dest="workflow_task_type")
    parser.add_argument("--workflow-complexity", type=str, default="", dest="workflow_complexity")
    parser.add_argument("--workflow-mode", type=str, default="", dest="workflow_mode")
    parser.add_argument("--workflow-file-plan", type=str, default="", dest="workflow_file_plan")
    parser.add_argument("--workflow-goal", type=str, default="", dest="workflow_goal")
    parser.add_argument("--workflow-constraints", type=str, default="", dest="workflow_constraints")
    parser.add_argument("--workflow-acceptance", type=str, default="", dest="workflow_acceptance")
    parser.add_argument("--workflow-research-reason", type=str, default="", dest="workflow_research_reason")
    parser.add_argument("--workflow-rollback-reason", type=str, default="", dest="workflow_rollback_reason")
    parser.add_argument("--workflow-strategy-note", type=str, default="", dest="workflow_strategy_note")

    # --update-mempalace / --bridge 參數
    parser.add_argument("--context", type=str, default="", dest="context")
    parser.add_argument("--output", type=str, default="", dest="output")
    parser.add_argument("--include-failed", action="store_true", dest="include_failed")

    # 模型覆寫
    parser.add_argument("--model", type=str, default=OLLAMA_MODEL, dest="model")

    args = parser.parse_args()

    if args.model != OLLAMA_MODEL_GOVERNOR:   # 僅在明確覆寫時才更改
        OLLAMA_MODEL_GOVERNOR   = args.model
        OLLAMA_MODEL_RESEARCHER = args.model
    OLLAMA_MODEL = OLLAMA_MODEL_GOVERNOR

    if args.init:
        cmd_init()
    elif args.record:
        cmd_record(
            status=args.status_code,
            task=args.task,
            diff_summary=args.diff_summary,
            harness_delta=args.harness_delta,
            files=[f for f in args.files.split(",") if f],
            iteration=args.iteration,
            max_iter=args.max_iter,
            aider_model=args.aider_model,
            harness_cmd=args.harness_cmd,
            work_dir=args.work_dir,
            prompt_excerpt=args.prompt_excerpt,
            harness_excerpt=args.harness_excerpt,
            selected_l4_ids=[f for f in args.selected_l4_ids.split(",") if f],
            selected_l5_ids=[f for f in args.selected_l5_ids.split(",") if f],
            patch_summary=args.patch_summary,
            task_complete=bool(args.task_complete),
            run_id=args.run_id,
            trace_dir=args.trace_dir,
            failure_mode=args.failure_mode,
            root_cause=args.root_cause,
            patch_type=args.patch_type,
            repo_type=args.repo_type,
            language=args.language,
            framework=args.framework,
            test_scope=args.test_scope,
            workflow_task_type=args.workflow_task_type,
            workflow_complexity=args.workflow_complexity,
            workflow_mode=args.workflow_mode,
            workflow_file_plan=args.workflow_file_plan,
            workflow_goal=args.workflow_goal,
            workflow_constraints=args.workflow_constraints,
            workflow_acceptance=args.workflow_acceptance,
            workflow_research_reason=args.workflow_research_reason,
            workflow_rollback_reason=args.workflow_rollback_reason,
            workflow_strategy_note=args.workflow_strategy_note,
        )
    elif args.update_mempalace:
        cmd_update_mempalace(context=args.context)
    elif args.sleep:
        cmd_sleep()
    elif args.deep:
        cmd_deep()
    elif args.wake:
        cmd_wake()
    elif args.decay:
        cmd_decay()
    elif args.bridge:
        cmd_bridge(mempalace_output=args.context)
    elif args.status:
        cmd_status()
    elif args.export_training_data:
        cmd_export_training_data(
            output_path=args.output,
            include_failed=args.include_failed,
        )
    elif args.score_strategies:
        cmd_score_strategies()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
