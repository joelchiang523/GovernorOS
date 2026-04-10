#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
git_diff_intel.py — Git Diff Intelligence 模組

每次 Aider 修改後記錄 diff，累積後萃取高風險 / 高勝率改動模式，寫入 L5。

指令：
  --record   記錄一筆 diff（由 main_loop.py 呼叫）
  --analyze  分析累積 records，萃取 pattern → 輸出 L5 候選（JSON）
  --status   顯示目前 diff 統計

Options for --record:
  --patch-type   refactor / bugfix / feature / config / test / other
  --files        修改的檔案（逗號分隔）
  --harness      pass / fail
  --delta        Harness 分數變化（float，可正可負）
  --lines        修改行數（int）
  --rollback     是否觸發 rollback（0/1）
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
MEMORY_DIR = BASE_DIR / "memory"
RECORDS_PATH = MEMORY_DIR / "git_diff_records.jsonl"

# 需要幾筆同類 record 才萃取 pattern
MIN_PATTERN_COUNT = 4


# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────

def append_record(record: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(RECORDS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_records() -> list:
    if not RECORDS_PATH.exists():
        return []
    records = []
    with open(RECORDS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# ─────────────────────────────────────────────
# Patch 分類輔助
# ─────────────────────────────────────────────

PATCH_KEYWORDS = {
    "refactor":  ["refactor", "rename", "reorganize", "cleanup", "restructure"],
    "bugfix":    ["fix", "bug", "error", "crash", "exception", "wrong", "incorrect"],
    "feature":   ["add", "new", "implement", "create", "introduce", "support"],
    "config":    ["config", "setting", "parameter", "constant", "threshold", "env"],
    "test":      ["test", "spec", "assert", "mock", "fixture", "coverage"],
}

def classify_patch(diff_text: str) -> str:
    """從 diff 文字猜測 patch_type，優先用使用者提供的值"""
    lower = diff_text.lower()
    for ptype, keywords in PATCH_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return ptype
    return "other"


def extract_core_files(files: list[str]) -> list[str]:
    """只保留檔名（不含路徑），方便做模式比對"""
    return [Path(f).name for f in files]


# ─────────────────────────────────────────────
# --record
# ─────────────────────────────────────────────

def cmd_record(
    patch_type: str,
    files: list[str],
    harness_result: str,
    harness_delta: float,
    lines_changed: int,
    rollback: bool,
    diff_text: str = "",
) -> None:
    if patch_type == "auto" and diff_text:
        patch_type = classify_patch(diff_text)

    record = {
        "time":          datetime.now().isoformat(),
        "patch_type":    patch_type,
        "files_changed": files,
        "core_files":    extract_core_files(files),
        "harness_result": harness_result,   # "pass" / "fail"
        "harness_delta": harness_delta,
        "lines_changed": lines_changed,
        "rollback":      rollback,
    }
    append_record(record)
    flag = "✓" if harness_result == "pass" else "✗"
    print(f"[DiffIntel] 記錄 {flag} | {patch_type} | {', '.join(files) or '(無檔案)'} | delta={harness_delta:+.1f}")


# ─────────────────────────────────────────────
# --analyze：從累積 records 萃取 L5 候選
# ─────────────────────────────────────────────

def _file_combo_key(core_files: list[str]) -> str:
    return "+".join(sorted(set(core_files)))


def analyze_patterns(records: list) -> list[dict]:
    """
    分析 records，回傳 L5 候選 list。
    策略：
      高風險組合 = 同時修改多個核心檔案且 fail_rate > 50%（>=4筆）
      高勝率組合 = 單一核心檔案修改且 pass_rate > 75%（>=4筆）
    """
    # 按「檔案組合」分群
    groups: dict[str, list] = defaultdict(list)
    for r in records:
        key = _file_combo_key(r.get("core_files", []))
        if key:
            groups[key].append(r)

    candidates = []

    for combo, recs in groups.items():
        if len(recs) < MIN_PATTERN_COUNT:
            continue

        total   = len(recs)
        passes  = sum(1 for r in recs if r.get("harness_result") == "pass")
        fails   = total - passes
        rollbacks = sum(1 for r in recs if r.get("rollback"))
        pass_rate = passes / total
        fail_rate = fails / total
        avg_delta = sum(r.get("harness_delta", 0) for r in recs) / total
        files_in_combo = combo.split("+")
        multi_file = len(files_in_combo) > 1

        # 高風險改動模式
        if fail_rate >= 0.5 and (multi_file or rollbacks >= 2):
            candidates.append({
                "condition": f"需要同時修改 {combo}",
                "action":    f"先為各檔案建立獨立測試，逐一修改後驗證，最後整合",
                "avoid":     f"不要一次同時修改 {combo}（歷史 fail_rate={fail_rate:.0%}，rollback={rollbacks}次）",
                "scope":     "git_diff_intel",
                "confidence": round(0.5 + fail_rate * 0.3, 2),
                "evidence_count": total,
                "source": "git_diff_intel",
                "pattern_type": "high_risk",
                "combo": combo,
            })

        # 高勝率改動模式
        if pass_rate >= 0.75 and not multi_file and avg_delta > 0:
            candidates.append({
                "condition": f"需要提升 {combo} 相關功能",
                "action":    f"直接修改 {combo}（歷史 pass_rate={pass_rate:.0%}，平均 delta={avg_delta:+.1f}）",
                "avoid":     f"不要在修改 {combo} 時同時動其他核心模組",
                "scope":     "git_diff_intel",
                "confidence": round(0.5 + pass_rate * 0.3, 2),
                "evidence_count": total,
                "source": "git_diff_intel",
                "pattern_type": "high_success",
                "combo": combo,
            })

    return candidates


def cmd_analyze(output_json: bool = False) -> None:
    records = load_records()
    if not records:
        print("[DiffIntel] 尚無 diff records")
        return

    candidates = analyze_patterns(records)

    if not candidates:
        print(f"[DiffIntel] 共 {len(records)} 筆 records，尚未達到 pattern 萃取門檻（每組需 >= {MIN_PATTERN_COUNT} 筆）")
        return

    if output_json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
    else:
        print(f"[DiffIntel] 分析 {len(records)} 筆 records，萃取 {len(candidates)} 個 pattern：")
        for c in candidates:
            tag = "⚠️  高風險" if c["pattern_type"] == "high_risk" else "✓  高勝率"
            print(f"  {tag} [{c['combo']}] conf={c['confidence']:.2f} n={c['evidence_count']}")
            print(f"       avoid: {c['avoid'][:60]}")


# ─────────────────────────────────────────────
# --status
# ─────────────────────────────────────────────

def cmd_status() -> None:
    records = load_records()
    if not records:
        print("[DiffIntel] 尚無 diff records")
        return

    total   = len(records)
    passes  = sum(1 for r in records if r.get("harness_result") == "pass")
    fails   = total - passes
    rollbacks = sum(1 for r in records if r.get("rollback"))

    by_type: dict[str, dict] = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in records:
        pt = r.get("patch_type", "other")
        result = r.get("harness_result", "fail")
        by_type[pt][result] += 1

    print("\n═══════════════════════════════════")
    print("  Git Diff Intelligence 統計")
    print("═══════════════════════════════════")
    print(f"  總記錄：{total} 筆  ✓{passes} ✗{fails}  rollback:{rollbacks}")
    print(f"  整體 pass rate：{passes/total:.0%}")
    print()
    print("  按 patch_type：")
    for pt, counts in sorted(by_type.items()):
        t = counts["pass"] + counts["fail"]
        rate = counts["pass"] / t if t else 0
        print(f"    {pt:<12} {t:>3}筆  pass={rate:.0%}")

    # 按檔案組合列出高風險
    groups: dict[str, list] = defaultdict(list)
    for r in records:
        key = _file_combo_key(r.get("core_files", []))
        if key:
            groups[key].append(r)

    risky = []
    for combo, recs in groups.items():
        if len(recs) < 3:
            continue
        fails_n = sum(1 for r in recs if r.get("harness_result") == "fail")
        if fails_n / len(recs) >= 0.5:
            risky.append((combo, fails_n, len(recs)))

    if risky:
        print()
        print("  ⚠️  高風險改動組合（fail >= 50%）：")
        for combo, f, t in sorted(risky, key=lambda x: -x[1]/x[2]):
            print(f"    {combo}  fail={f}/{t}（{f/t:.0%}）")

    print("═══════════════════════════════════\n")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    """
    純 flag 式 CLI，與 main_loop.py 的呼叫方式完全相容：
      python git_diff_intel.py --record --patch-type=bugfix --files=a.py ...
      python git_diff_intel.py --analyze [--json]
      python git_diff_intel.py --status
    """
    parser = argparse.ArgumentParser(description="git_diff_intel.py — Git Diff Intelligence")

    # 動作旗標（三選一）
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--record",  action="store_true", help="記錄一筆 diff")
    action.add_argument("--analyze", action="store_true", help="分析 patterns")
    action.add_argument("--status",  action="store_true", help="顯示統計")

    # --record 專用參數
    parser.add_argument("--patch-type", default="auto",
                        choices=["refactor","bugfix","feature","config","test","other","auto"])
    parser.add_argument("--files",     default="", help="修改的檔案，逗號分隔")
    parser.add_argument("--harness",   default="fail", choices=["pass","fail"])
    parser.add_argument("--delta",     type=float, default=0.0)
    parser.add_argument("--lines",     type=int,   default=0)
    parser.add_argument("--rollback",  type=int,   default=0)
    parser.add_argument("--diff-text", default="", dest="diff_text")

    # --analyze 專用參數
    parser.add_argument("--json", action="store_true", dest="output_json")

    args = parser.parse_args()

    if args.record:
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        cmd_record(
            patch_type=args.patch_type,
            files=files,
            harness_result=args.harness,
            harness_delta=args.delta,
            lines_changed=args.lines,
            rollback=bool(args.rollback),
            diff_text=args.diff_text,
        )
    elif args.analyze:
        cmd_analyze(output_json=args.output_json)
    elif args.status:
        cmd_status()


if __name__ == "__main__":
    main()
