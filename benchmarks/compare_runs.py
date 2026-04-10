#!/usr/bin/env python3
"""
compare_runs.py — 比較兩次 benchmark 結果，顯示能力變化

用法：
  python3 compare_runs.py                         # 比較最新兩次
  python3 compare_runs.py run_A.json run_B.json   # 指定報告
"""
import json
import sys
from pathlib import Path

REPORTS_DIR = Path(__file__).parent.parent / "benchmark_reports"


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_latest_reports(n: int = 2) -> list[Path]:
    reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return reports[-n:]


def compare(a: dict, b: dict) -> None:
    print(f"\n{'═'*58}")
    print(f"  Benchmark 比較報告")
    print(f"{'═'*58}")
    print(f"  舊：{a['suite']}  {a['generated_at'][:16]}")
    print(f"  新：{b['suite']}  {b['generated_at'][:16]}")
    print(f"{'─'*58}")

    # 整體指標
    pa = a["pass_rate"];  pb = b["pass_rate"]
    ia = a["avg_iterations"]; ib = b["avg_iterations"]
    ra = a["total_rollbacks"]; rb = b["total_rollbacks"]

    def arrow(old, new, higher_is_better=True):
        if new > old:
            return "↑" if higher_is_better else "↓⚠"
        if new < old:
            return "↓⚠" if higher_is_better else "↑"
        return "→"

    print(f"  {'指標':<20} {'舊':>8} {'新':>8}  {'變化':>6}")
    print(f"  {'─'*50}")
    print(f"  {'pass_rate':<20} {pa:>7.1%} {pb:>7.1%}  {arrow(pa,pb)} {(pb-pa):+.1%}")
    print(f"  {'avg_iterations':<20} {ia:>8.2f} {ib:>8.2f}  {arrow(ia,ib,False)} {(ib-ia):+.2f}")
    print(f"  {'total_rollbacks':<20} {ra:>8d} {rb:>8d}  {arrow(ra,rb,False)} {(rb-ra):+d}")
    print(f"{'─'*58}")

    # 逐題比較
    cases_a = {c["id"]: c for c in a.get("cases", [])}
    cases_b = {c["id"]: c for c in b.get("cases", [])}
    all_ids = sorted(set(cases_a) | set(cases_b))

    print(f"  {'題目':<16} {'舊':>6} {'新':>6}  {'迭代(舊→新)':>14}  {'變化'}")
    print(f"  {'─'*56}")
    improved = failed = unchanged = 0
    for cid in all_ids:
        ca = cases_a.get(cid)
        cb = cases_b.get(cid)
        if ca is None or cb is None:
            print(f"  {cid:<16} {'N/A':>6} {'N/A':>6}  (僅一次出現)")
            continue
        old_pass = "PASS" if ca["passed"] else "FAIL"
        new_pass = "PASS" if cb["passed"] else "FAIL"
        old_iter = ca.get("iterations", 0)
        new_iter = cb.get("iterations", 0)
        if ca["passed"] == cb["passed"]:
            change = "→ 相同"
            unchanged += 1
        elif cb["passed"]:
            change = "✓ 改善"
            improved += 1
        else:
            change = "✗ 退步"
            failed += 1
        print(f"  {cid:<16} {old_pass:>6} {new_pass:>6}  {old_iter:>5}→{new_iter:<5}  {change}")

    print(f"{'─'*58}")
    print(f"  改善：{improved}  退步：{failed}  相同：{unchanged}")
    print(f"{'═'*58}\n")


def main():
    if len(sys.argv) == 3:
        a_path = Path(sys.argv[1])
        b_path = Path(sys.argv[2])
    else:
        latest = find_latest_reports(2)
        if len(latest) < 2:
            print("需要至少 2 次 benchmark 結果才能比較。")
            print(f"目前找到 {len(latest)} 個報告：{[p.name for p in latest]}")
            sys.exit(1)
        a_path, b_path = latest[0], latest[1]

    compare(load_report(a_path), load_report(b_path))


if __name__ == "__main__":
    main()
