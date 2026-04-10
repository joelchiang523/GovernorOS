#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mempalace.py — MemPalace 四區管理工具

MemPalace 是 GovernorOS 的即時任務工作記憶，存於 CLAUDE.md。
四區：[GOAL] [DONE] [PENDING] [CONSTRAINTS]

使用方式：
  python3 mempalace.py show                          # 顯示目前四區
  python3 mempalace.py set-goal "修正登入 bug..."    # 設定目標
  python3 mempalace.py done "完成 auth.py 修正"      # 新增完成項目
  python3 mempalace.py pending "等待 harness 通過"   # 新增待辦
  python3 mempalace.py constraint "只修改 auth.py"   # 新增限制
  python3 mempalace.py clear-done                    # 清空已完成
  python3 mempalace.py reset                         # 重置四區（新任務）
  python3 mempalace.py from-spec spec.json           # 從 task_intake 輸出注入
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR  = Path(__file__).parent
CLAUDE_MD = BASE_DIR / "CLAUDE.md"

C = {
    "reset": "\033[0m", "bold": "\033[1m",
    "cyan": "\033[96m", "green": "\033[92m",
    "yellow": "\033[93m", "red": "\033[91m",
    "gray": "\033[90m",
}
def c(text, key): return f"{C[key]}{text}{C['reset']}"


# ── 四區讀寫 ─────────────────────────────────────────────
ZONE_TAGS = {
    "goal":        ("[GOAL]",        "[DONE]"),
    "done":        ("[DONE]",        "[PENDING]"),
    "pending":     ("[PENDING]",     "[CONSTRAINTS]"),
    "constraints": ("[CONSTRAINTS]", "<!-- MEMPALACE_END -->"),
}

def read_mempalace() -> dict[str, str]:
    content = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else ""
    zones = {}
    for zone, (start, end) in ZONE_TAGS.items():
        m = re.search(re.escape(start) + r"\n(.*?)(?=" + re.escape(end) + r")", content, re.DOTALL)
        zones[zone] = m.group(1).strip() if m else ""
    return zones


def write_mempalace(zones: dict[str, str]) -> None:
    if not CLAUDE_MD.exists():
        print(c(f"[ERROR] CLAUDE.md 不存在：{CLAUDE_MD}", "red"))
        sys.exit(1)
    content = CLAUDE_MD.read_text(encoding="utf-8")
    for zone, (start, end) in ZONE_TAGS.items():
        body = zones.get(zone, "")
        pattern = re.compile(
            re.escape(start) + r"\n.*?(?=" + re.escape(end) + r")",
            re.DOTALL,
        )
        replacement = f"{start}\n{body}\n" if body else f"{start}\n"
        content = pattern.sub(replacement, content)
    CLAUDE_MD.write_text(content, encoding="utf-8")


# ── 顯示 ─────────────────────────────────────────────────
def cmd_show():
    zones = read_mempalace()
    print(c(f"\n{'═'*54}", "cyan"))
    print(c("  MemPalace — 四區任務記憶", "bold"))
    print(c(f"{'═'*54}", "cyan"))
    labels = {"goal": "GOAL（當前目標）", "done": "DONE（已完成）",
              "pending": "PENDING（待辦）", "constraints": "CONSTRAINTS（限制）"}
    for zone, label in labels.items():
        body = zones[zone]
        print(c(f"\n  [{label}]", "bold"))
        if body:
            for line in body.splitlines():
                print(f"    {line}")
        else:
            print(c("    （空）", "gray"))
    print(c(f"\n{'═'*54}\n", "cyan"))


# ── 設定 GOAL ────────────────────────────────────────────
def cmd_set_goal(goal_text: str):
    zones = read_mempalace()
    zones["goal"] = goal_text
    write_mempalace(zones)
    print(c(f"✓ [GOAL] 已更新", "green"))


# ── 新增 DONE 項目 ────────────────────────────────────────
def cmd_done(item: str):
    zones = read_mempalace()
    existing = zones.get("done", "")
    n = len([l for l in existing.splitlines() if l.strip()]) + 1
    new_line = f"{n}. {item}"
    zones["done"] = (existing + "\n" + new_line).strip()
    write_mempalace(zones)
    print(c(f"✓ [DONE] 新增：{new_line}", "green"))


# ── 新增 PENDING 項目 ─────────────────────────────────────
def cmd_pending(item: str):
    zones = read_mempalace()
    existing = zones.get("pending", "")
    n = len([l for l in existing.splitlines() if l.strip()]) + 1
    new_line = f"{n}. {item}"
    zones["pending"] = (existing + "\n" + new_line).strip()
    write_mempalace(zones)
    print(c(f"✓ [PENDING] 新增：{new_line}", "green"))


# ── 新增 CONSTRAINT ───────────────────────────────────────
def cmd_constraint(item: str):
    zones = read_mempalace()
    existing = zones.get("constraints", "")
    n = len([l for l in existing.splitlines() if l.strip()]) + 1
    new_line = f"{n}. {item}"
    zones["constraints"] = (existing + "\n" + new_line).strip()
    write_mempalace(zones)
    print(c(f"✓ [CONSTRAINTS] 新增：{new_line}", "green"))


# ── 完成任務：PENDING → DONE ──────────────────────────────
def cmd_complete(index: int):
    zones = read_mempalace()
    pending_lines = [l for l in zones.get("pending", "").splitlines() if l.strip()]
    if index < 1 or index > len(pending_lines):
        print(c(f"[ERROR] PENDING 項目 #{index} 不存在（共 {len(pending_lines)} 項）", "red"))
        return
    item = pending_lines.pop(index - 1)
    item_text = re.sub(r"^\d+\.\s*", "", item)
    # 重新編號
    strip_num = lambda l: re.sub(r'^\d+\.\s*', '', l)
    zones["pending"] = "\n".join(f"{i+1}. {strip_num(l)}" for i, l in enumerate(pending_lines))
    done_existing = zones.get("done", "")
    done_n = len([l for l in done_existing.splitlines() if l.strip()]) + 1
    zones["done"] = (done_existing + f"\n{done_n}. {item_text}").strip()
    write_mempalace(zones)
    print(c(f"✓ 已將 PENDING #{index} 移至 DONE：{item_text}", "green"))


# ── 清空 ──────────────────────────────────────────────────
def cmd_clear_done():
    zones = read_mempalace()
    zones["done"] = ""
    write_mempalace(zones)
    print(c("✓ [DONE] 已清空", "green"))


def cmd_reset():
    zones = read_mempalace()
    zones.update({"goal": "（等待新任務）", "done": "", "pending": "", "constraints": ""})
    write_mempalace(zones)
    print(c("✓ MemPalace 四區已重置", "green"))


# ── 從 task_intake spec JSON 注入 ────────────────────────
def cmd_from_spec(spec_path: str):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    goal = spec.get("goal", "")
    constraints = spec.get("constraints", [])
    acceptance  = spec.get("acceptance", [])
    deploy      = spec.get("deploy_method", "")
    files       = ", ".join(spec.get("aider_files", [])) if spec.get("aider_files") != ["auto"] else "auto"
    harness     = spec.get("harness_cmd", "auto")

    goal_text = (
        f"{goal}\n"
        f"deploy={deploy}  harness={harness}  files={files}"
    )
    constraints_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(constraints))
    pending_text = "\n".join(f"{i+1}. {a}" for i, a in enumerate(acceptance))

    zones = read_mempalace()
    zones["goal"]        = goal_text
    zones["pending"]     = pending_text or "等待任務執行"
    zones["constraints"] = constraints_text
    zones["done"]        = ""
    write_mempalace(zones)
    print(c(f"✓ MemPalace 已從 spec 注入：{spec_path}", "green"))
    cmd_show()


# ── 主程式 ───────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args or args[0] in ("show", "status"):
        cmd_show(); return
    cmd = args[0]
    text = " ".join(args[1:]) if len(args) > 1 else ""

    dispatch = {
        "set-goal":   lambda: cmd_set_goal(text),
        "done":       lambda: cmd_done(text),
        "pending":    lambda: cmd_pending(text),
        "constraint": lambda: cmd_constraint(text),
        "complete":   lambda: cmd_complete(int(text)),
        "clear-done": cmd_clear_done,
        "reset":      cmd_reset,
        "from-spec":  lambda: cmd_from_spec(text),
    }
    fn = dispatch.get(cmd)
    if fn:
        fn()
    else:
        print(f"未知指令：{cmd}")
        print("可用指令：show | set-goal | done | pending | constraint | complete N | clear-done | reset | from-spec")


if __name__ == "__main__":
    main()
