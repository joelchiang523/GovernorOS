#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
task_intake.py — 對話式任務注入 + MemPalace 更新

使用方式：
  python3 task_intake.py                  # 互動對話輸入任務
  python3 task_intake.py --auto-start     # 注入後自動啟動 main_loop.py
  python3 task_intake.py --show           # 顯示目前 GOAL

流程：
  1. 使用者用自然語言描述需求
  2. Governor 模型（qwen3.5:27b）解析成結構化 task spec
  3. 自動辨識：任務類型、部署方式、需修改檔案、harness 指令
  4. 寫入 CLAUDE.md MemPalace [GOAL] 與 PROMPT.md GOAL_INJECT
  5. （可選）直接啟動 main_loop.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE_DIR   = Path(__file__).parent
CLAUDE_MD  = BASE_DIR / "CLAUDE.md"
PROMPT_MD  = BASE_DIR / "PROMPT.md"
OLLAMA_URL = "http://localhost:11434/api/generate"
_GOVERNOR_DEFAULT = os.environ.get("OLLAMA_MODEL_GOVERNOR", "qwen3.5:27b")
GOVERNOR = _GOVERNOR_DEFAULT

# ── ANSI 顏色 ────────────────────────────────────────────
C_RESET  = "\033[0m"
C_CYAN   = "\033[96m"
C_GREEN  = "\033[92m"
C_YELLOW = "\033[93m"
C_RED    = "\033[91m"
C_BOLD   = "\033[1m"

def c(text, color): return f"{color}{text}{C_RESET}"


# ── Ollama 呼叫 ───────────────────────────────────────────
def ollama_call(prompt: str, model: str = GOVERNOR, timeout: int = 180) -> str:
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print(c(f"[ERROR] Ollama 呼叫失敗：{e}", C_RED))
        return ""


# ── 任務解析（Governor 負責） ────────────────────────────
PARSE_PROMPT = """你是 GovernorOS 的任務規劃代理。

使用者描述了一個開發任務。請將它解析為結構化格式，並以純 JSON 輸出（不要有任何說明文字）。

使用者輸入：
{user_input}

對話歷史補充（若有）：
{history}

請輸出以下 JSON 結構（所有欄位必須填寫，不可省略）：
{{
  "goal": "一句話說明任務目標（50字以內）",
  "task_type": "bugfix | feature | refactor | test_task | config | investigation | general",
  "deploy_method": "local_run | github_pages | docker | pytest | node_script | apps_script | none",
  "work_dir": "專案根目錄絕對路徑（若未提及填 auto）",
  "aider_files": ["需要修改的檔案列表，若未明確提及填 auto"],
  "harness_cmd": "驗收指令（pytest/node/bash 等；若無法確定填 auto）",
  "max_iter": 8,
  "constraints": ["限制條件列表"],
  "acceptance": ["驗收條件列表"],
  "clarify_needed": ["若有需要向使用者確認的問題，列在此處；若資訊足夠則為空列表"]
}}

判斷規則：
- 若提到 pytest/測試/test → harness_cmd 用 pytest
- 若提到 GitHub Pages/靜態部署 → deploy_method=github_pages
- 若提到 Google Apps Script → deploy_method=apps_script
- 若提到 Docker/容器 → deploy_method=docker
- 若提到 node/js/npm → harness_cmd 用 node
- 若提到本地執行/跑程式 → deploy_method=local_run
- work_dir 若使用者有提到路徑就填入，否則填 auto
- aider_files 若使用者有提到特定檔案就填入，否則填 ["auto"]
"""

def parse_task(user_input: str, history: str = "") -> dict | None:
    prompt = PARSE_PROMPT.format(user_input=user_input, history=history)
    raw = ollama_call(prompt)
    if not raw:
        return None
    # 提取 JSON（允許被 markdown 包覆）
    json_match = re.search(r'\{[\s\S]+\}', raw)
    if not json_match:
        print(c(f"[WARN] 無法解析 JSON，原始輸出：\n{raw[:300]}", C_YELLOW))
        return None
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError as e:
        print(c(f"[WARN] JSON 解析錯誤：{e}", C_YELLOW))
        return None


# ── MemPalace 注入 ────────────────────────────────────────
def replace_block(content: str, start_tag: str, end_tag: str, new_body: str) -> str:
    pattern = re.compile(
        re.escape(start_tag) + r".*?" + re.escape(end_tag),
        re.DOTALL,
    )
    replacement = f"{start_tag}\n{new_body}\n{end_tag}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    return content + f"\n{start_tag}\n{new_body}\n{end_tag}\n"


def build_goal_block(spec: dict) -> str:
    constraints_str = "\n".join(f"{i+1}. {c}" for i, c in enumerate(spec.get("constraints", []))) or "（無）"
    acceptance_str  = "\n".join(f"{i+1}. {a}" for i, a in enumerate(spec.get("acceptance", []))) or "（無）"
    files_str = ", ".join(spec.get("aider_files", ["auto"])) if spec.get("aider_files") != ["auto"] else "（待自動偵測）"
    harness = spec.get("harness_cmd", "auto")
    harness_str = harness if harness != "auto" else "（待確認）"

    return f"""**任務**：{spec['goal']}

**類型**：{spec.get('task_type', 'general')}  **部署**：{spec.get('deploy_method', 'none')}
**目錄**：{spec.get('work_dir', 'auto')}
**檔案**：{files_str}
**驗收**：{harness_str}

**限制條件**：
{constraints_str}

**驗收標準**：
{acceptance_str}

完成後輸出 TASK_COMPLETE"""


def inject_to_claude_md(goal_block: str, spec: dict) -> bool:
    if not CLAUDE_MD.exists():
        print(c(f"[WARN] CLAUDE.md 不存在：{CLAUDE_MD}", C_YELLOW))
        return False
    content = CLAUDE_MD.read_text(encoding="utf-8")
    content = replace_block(content, "[GOAL]", "[DONE]",
        goal_block + "\n[DONE]".split("[DONE]")[0] if "[DONE]" in content else goal_block)
    # 直接替換 GOAL 區塊（MemPalace 格式）
    content = re.sub(
        r'\[GOAL\].*?(\[DONE\])',
        f'[GOAL]\n{goal_block}\n[DONE]',
        content, flags=re.DOTALL
    )
    CLAUDE_MD.write_text(content, encoding="utf-8")
    return True


def inject_to_prompt_md(goal_block: str) -> bool:
    if not PROMPT_MD.exists():
        print(c(f"[WARN] PROMPT.md 不存在：{PROMPT_MD}", C_YELLOW))
        return False
    content = PROMPT_MD.read_text(encoding="utf-8")
    content = replace_block(content, "<!-- GOAL_INJECT_START -->", "<!-- GOAL_INJECT_END -->", goal_block)
    PROMPT_MD.write_text(content, encoding="utf-8")
    return True


# ── 對話主循環 ────────────────────────────────────────────
def chat_loop() -> dict | None:
    print(c(f"\n{'═'*56}", C_CYAN))
    print(c("  GovernorOS — 任務輸入介面", C_BOLD))
    print(c("  描述你想完成的任務，系統自動解析並注入 MemPalace", C_CYAN))
    print(c(f"  模型：{GOVERNOR}", C_CYAN))
    print(c(f"{'═'*56}\n", C_CYAN))

    history_parts = []
    current_spec = None

    while True:
        try:
            user_input = input(c("你 › ", C_GREEN)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return None

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q", "離開"):
            return None
        if user_input.lower() in ("ok", "確認", "yes", "y") and current_spec:
            return current_spec

        history_str = "\n".join(history_parts[-6:])
        history_parts.append(f"使用者：{user_input}")

        print(c("\n[解析中...]", C_YELLOW))
        spec = parse_task(user_input, history_str)
        if not spec:
            print(c("解析失敗，請重新描述或換一種說法。\n", C_RED))
            continue

        current_spec = spec

        # 顯示解析結果
        print(c(f"\n{'─'*56}", C_CYAN))
        print(c("  解析結果", C_BOLD))
        print(c(f"{'─'*56}", C_CYAN))
        print(f"  目標    : {spec.get('goal', '')}")
        print(f"  類型    : {spec.get('task_type', '')}  部署：{spec.get('deploy_method', '')}")
        print(f"  目錄    : {spec.get('work_dir', 'auto')}")
        files = spec.get("aider_files", [])
        print(f"  檔案    : {', '.join(files) if files != ['auto'] else '（待偵測）'}")
        harness = spec.get("harness_cmd", "auto")
        print(f"  驗收    : {harness if harness != 'auto' else '（待確認）'}")
        constraints = spec.get("constraints", [])
        if constraints:
            print(f"  限制    : {' / '.join(constraints[:3])}")
        print(c(f"{'─'*56}", C_CYAN))

        # 需要澄清的問題
        clarify = spec.get("clarify_needed", [])
        if clarify:
            print(c("\n  還需要確認：", C_YELLOW))
            for q in clarify:
                print(f"  • {q}")
            print(c("\n  請補充說明，或輸入「確認」直接使用現有解析結果。\n", C_YELLOW))
        else:
            print(c("\n  輸入「確認」注入任務，或補充說明修改解析結果。\n", C_GREEN))

        history_parts.append(f"系統解析：goal={spec.get('goal','')}, type={spec.get('task_type','')}, harness={spec.get('harness_cmd','')}")


# ── 顯示目前 GOAL ─────────────────────────────────────────
def show_current_goal():
    content = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else ""
    match = re.search(r'\[GOAL\](.*?)\[DONE\]', content, re.DOTALL)
    if match:
        print(c("\n目前 MemPalace [GOAL]：", C_BOLD))
        print(match.group(1).strip())
    else:
        print(c("CLAUDE.md 中找不到 [GOAL] 區塊。", C_YELLOW))
    print()


# ── 主程式 ────────────────────────────────────────────────
def main():
    global GOVERNOR
    import argparse
    parser = argparse.ArgumentParser(description="GovernorOS 對話式任務注入")
    parser.add_argument("--auto-start", action="store_true", help="注入後自動啟動 main_loop.py")
    parser.add_argument("--show",       action="store_true", help="顯示目前 GOAL 後退出")
    parser.add_argument("--model",      default=GOVERNOR,    help="覆蓋 Governor 模型")
    args = parser.parse_args()

    if args.show:
        show_current_goal()
        return

    GOVERNOR = args.model

    spec = chat_loop()
    if not spec:
        return

    goal_block = build_goal_block(spec)

    # 注入到 MemPalace
    ok_claude = inject_to_claude_md(goal_block, spec)
    ok_prompt = inject_to_prompt_md(goal_block)

    print(c(f"\n{'═'*56}", C_GREEN))
    print(c("  任務已注入 MemPalace", C_BOLD))
    print(f"  CLAUDE.md : {'✓' if ok_claude else '✗'}")
    print(f"  PROMPT.md : {'✓' if ok_prompt else '✗'}")
    print(c(f"{'═'*56}\n", C_GREEN))

    # 自動啟動
    if args.auto_start:
        work_dir = spec.get("work_dir", "auto")
        harness  = spec.get("harness_cmd", "auto")
        files    = spec.get("aider_files", [])
        max_iter = spec.get("max_iter", 8)

        if work_dir == "auto" or harness == "auto":
            print(c("[WARN] work_dir 或 harness 尚未確定，無法自動啟動。請手動執行 main_loop.py。", C_YELLOW))
            return

        cmd = [
            sys.executable, str(BASE_DIR / "main_loop.py"),
            "--task",    goal_block.split("\n")[0].replace("**任務**：", ""),
            "--harness", harness,
            "--work-dir", work_dir,
            "--max-iter", str(max_iter),
        ]
        if files and files != ["auto"]:
            cmd += ["--aider-files", " ".join(files)]

        print(c(f"啟動 main_loop.py...", C_CYAN))
        print(f"  {' '.join(cmd[:6])} ...\n")
        subprocess.run(cmd, cwd=BASE_DIR)


if __name__ == "__main__":
    main()
