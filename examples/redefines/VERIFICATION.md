# REDEFINES example — verification

`wvload.pco`: a flat `01 WV-DATA PIC X(4096)`, then `01 WV-CUTBLA-VIEW
REDEFINES WV-DATA` with **two `03` groups**. Both `01` lines are typed in
column 1, so the level number sits in the card-image sequence area. The
changing field (`WV-VALUE`) is in the **second** `03`.

```bash
python -m cobol_impact_analyzer --spec examples/redefines/change_spec.json --out out
```

Expected — 1 program, `CRITICAL 2, MEDIUM 2, INFO 2`, no coverage warnings:

| Field | Verdict | Why |
| --- | --- | --- |
| `WV-VALUE` | CRITICAL | `S9(10)V9(2) -> S9(13)V9(2)`, from the widened `CB_VALUE` |
| `WV-CUST-NAME` | CRITICAL | `X(30) -> X(50)`, from the widened `CB_NAME` |
| `WV-CUTBLA-KEY` | MEDIUM | second `03` — length grows `42 -> 62` |
| `WV-CUTBLA-VIEW` | MEDIUM | the `01` — length grows `50 -> 70`; **note:** overlays `01 WV-DATA PIC X(4096)` — 4096 bytes, still fits |
| `WV-DATA` | *no change* | the trace climbs `05 -> 03 -> 01`, crosses the REDEFINES, and finds `WV-DATA` already reserves 4096 bytes — far more than the grown 70. It is **named in the note above**, not reported as an edit. |

Nested `03`s trace fine: the field in the second `03` reaches its `03`, then
the `01`, then the REDEFINES.

A `REDEFINES` is an overlay, not an accumulation. The overlaid item is only
flagged when the **grown record is larger than it** — e.g. if `WV-DATA` were
`PIC X(60)` here it would be reported `X(60) -> X(70)`. A wide buffer that
already covers the record is a note, because there is nothing to do to it.

## If a real source still misbehaves

Send, renamed only, with `cat -A` (so tabs `^I` and line ends `$` show): the
buffer line, the `REDEFINES` line, and the **three lines above the buffer**.
Specifically: does the buffer line end in a period, does the line above it,
and is the real `PIC` exactly `X(nnnn)` or something else (`9(18)`, `N(...)`,
a `USAGE`, a `VALUE`, an inline comment)?

`git log --oneline -1` should be at or after the commit that adds this file.
