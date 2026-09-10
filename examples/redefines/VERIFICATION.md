# REDEFINES example — verification

A flat buffer and an `01` that redefines it with a typed layout, **both `01`
lines typed in column 1** so the level number falls in the card-image
sequence area. Inside the view, a numeric field and a name field are widened
at the source columns. The point: the analyzer must still see `WV-DATA` and
report it, and it must name the overlap on the layout findings.

```bash
python -m cobol_impact_analyzer --spec examples/redefines/change_spec.json --out out
```

Expected — 1 program, `CRITICAL 2, HIGH 1, MEDIUM 2, INFO 2`, no coverage
warnings:

| Field | Verdict | Why |
| --- | --- | --- |
| `WV-VALUE` | CRITICAL | `S9(10)V9(2) -> S9(13)V9(2)`, fetched from the widened `CB_VALUE` |
| `WV-CUST-NAME` | CRITICAL | `X(30) -> X(50)`, fetched from the widened `CB_NAME` |
| `WV-CUTBLA-KEY` | MEDIUM | group length grows `42 -> 62` |
| `WV-CUTBLA-VIEW` | MEDIUM | group length grows `42 -> 62`; **note:** overlays `01 WV-DATA PIC X(4096)` — 4096 bytes, still fits |
| `WV-DATA` | HIGH | `X(4096) -> X(4116)` — the buffer the view redefines; **note:** overlays `01 WV-CUTBLA-VIEW REDEFINES WV-DATA` — 42 bytes, smaller than 4116, check it |

`WV-DATA` also appears in **Changes by module** for `wvload.pco`.

## If this works here but not on your source

The difference is in your file. Send, renamed only:

```bash
sed -n '<buf-3>,<view+1>p' your_module.pco | cat -A
```

so the buffer line, the `REDEFINES` line, and the **three lines above the
buffer** are visible with tabs (`^I`) and line ends (`$`). Specifically: does
the buffer line end in a period, does the line above it, and is the real
`PIC` exactly `X(nnnn)` or something else (`9(18)`, `N(...)`, a `USAGE`, a
`VALUE`, an inline comment)?

## If it also fails here

You are on stale code. `git log --oneline -1` should be `827aba8` or later.
