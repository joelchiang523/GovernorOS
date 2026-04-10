# GovernorOS — Claude Working Memory

> The injection blocks below are auto-maintained by `dream_cycle.py`.
> Do not manually edit the content inside `<!-- *_INJECT_START/END -->` blocks.

---

## MemPalace Four Zones (Current Task State)

<!-- MEMPALACE_START -->
[GOAL]
（由 dream_cycle.py 注入 / Injected by dream_cycle.py）

[DONE]
（已完成項目 / Completed items）

[PENDING]
（待完成項目 / Pending items）

[CONSTRAINTS]
（執行約束 / Execution constraints）
<!-- MEMPALACE_END -->

---

## L4 Knowledge Injection (Today's Learned Patterns)

<!-- L4_INJECT_START -->
（尚無足夠知識 / No patterns yet）
<!-- L4_INJECT_END -->

---

## L5 Strategy Injection (Today's Execution Strategies)

<!-- L5_INJECT_START -->
（尚無足夠策略 / No strategies yet）
<!-- L5_INJECT_END -->

---

## System Rules (Non-overridable)

1. **Harness is the only truth**: Code change success/failure is determined by Harness results, not subjective judgement.
2. **Three consecutive failures trigger rollback**: 3× Harness fail in a row → auto git rollback, stop blind retries.
3. **Every iteration must be recorded**: After each Aider run, record an Episode (`dream_cycle.py --record`).
4. **Research has a word limit**: Autoresearch output is strictly limited to 200 words to prevent context pollution.
5. **L4 stores only patterns, L5 stores only steps**: The two are strictly separated; Bridge handles the conversion.
