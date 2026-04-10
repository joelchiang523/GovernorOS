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

OLLAMA_URL = "http://localhost:11434/api/generate"

# ── 雙模型分工（與 dream_cycle.py 保持一致）──────────────
# Governor：狀態機判斷 / Harness 解讀 / MemPalace 治理
OLLAMA_MODEL_GOVERNOR   = os.environ.get(
    "OLLAMA_MODEL_GOVERNOR", "qwen3.5:27b"
)
# Researcher：Autoresearch 200字 brief（高服從、快速產出）
OLLAMA_MODEL_RESEARCHER = os.environ.get(
    "OLLAMA_MODEL_RESEARCHER",
    "prutser/gemma-4-26B-A4B-it-ara-abliterated:q5_k_m",
)
OLLAMA_MODEL = OLLAMA_MODEL_GOVERNOR  # 向後相容


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
        if task_complete:
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

    task_complete = "TASK_COMPLETE" in output
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


def record_episode(task: str, harness_pass: bool, diff_summary: str, delta: float):
    status_code = 0 if harness_pass else 1
    dc([
        "--record",
        f"--task={task[:200]}",
        f"--status-code={status_code}",
        f"--diff-summary={diff_summary[:300]}",
        f"--harness-delta={delta:.2f}",
    ])


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

    print(f"\n{'═'*60}")
    print(f"  AI System v3.0 主控迴圈啟動")
    print(f"  任務：{task}")
    print(f"  Harness：{harness_cmd}")
    print(f"  最大迭代：{max_iter}")
    print(f"{'═'*60}\n")

    # 取得 Harness baseline
    print("[Loop] 執行 Harness baseline...")
    base_pass, baseline_score, _ = run_harness(harness_cmd, work_dir, score_pattern)
    prev_score = baseline_score
    print(f"[Loop] Baseline score={baseline_score:.2f}  pass={base_pass}")

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
            git_rollback(work_dir, target_tag=last_good_snapshot)
            sm.on_rollback_done()
            research_brief = ""
            log_lines.append(
                f"iter={iteration} ROLLBACK → {last_good_snapshot or 'HEAD^'}"
            )
            continue

        # ── Autoresearch ──────────────────────────
        if sm.state == "switch_tool":
            print(f"[Loop] 切換至 Autoresearch（連敗 {sm.consecutive_fails} 次）")
            # 讀取 L2（唯一真相），不讀 CLAUDE.md 展示層
            l2_path = BASE_DIR / "memory" / "L2_working.md"
            context_snapshot = (
                l2_path.read_text(encoding="utf-8")[:600]
                if l2_path.exists() else ""
            )
            research_brief = autoresearch(task, last_harness_output, context_snapshot)
            if research_brief:
                print(f"[Research] Brief（{len(research_brief)}字）：\n{research_brief}\n")
            sm.on_research_done()
            log_lines.append(f"iter={iteration} RESEARCH")
            continue

        # ── 組合給 Aider 的完整指令（讀 L2 唯一真相）──
        l2_path   = BASE_DIR / "memory" / "L2_working.md"
        mempalace = (
            l2_path.read_text(encoding="utf-8")[:800]
            if l2_path.exists() else ""
        )
        aider_message = (
            f"[ITER {iteration}/{max_iter}] 任務：{task}\n"
            + (f"\n[Research Brief]\n{research_brief}\n" if research_brief else "")
            + (f"\n[MemPalace]\n{mempalace}\n" if mempalace else "")
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

        # ── Snapshot（Harness 前建立，rollback 精確點）─
        snapshot_tag = git_snapshot(work_dir, iteration)

        # ── Harness 驗證 ──────────────────────────
        harness_pass, current_score, last_harness_output = run_harness(
            harness_cmd, work_dir, score_pattern
        )
        harness_delta  = current_score - prev_score
        harness_result = "pass" if harness_pass else "fail"
        if current_score > 0:
            prev_score = current_score

        # ── 更新 last_good_snapshot ───────────────
        if harness_pass:
            last_good_snapshot = snapshot_tag
            print(f"[Git] last_good_snapshot 更新 → {snapshot_tag}")

        # ── 記錄 Git Diff Intel ───────────────────
        patch_type = "bugfix" if sm.consecutive_fails > 0 else "feature"
        record_diff_intel(
            patch_type, changed_files, harness_result,
            harness_delta, lines_changed, rollback=False,
        )

        # ── Episode Scoring → L3 ─────────────────
        record_episode(task, harness_pass, diff_summary, harness_delta)

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
                        help="Researcher 模型（Autoresearch，預設 gemma-4-26B...）")
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

    main_loop(
        task         = args.task,
        harness_cmd  = args.harness,
        aider_model  = args.aider_model,
        aider_files  = aider_files,
        work_dir     = work_dir,
        max_iter     = args.max_iter,
        score_pattern= args.score_pattern or None,
        dry_run      = args.dry_run,
    )


if __name__ == "__main__":
    main()
