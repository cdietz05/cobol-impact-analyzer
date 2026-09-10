# CUTBLA widening — changes by file

_Generated 2026-09-10 18:36:08Z._

## Requested change

- `CUTBLA.CB_VALUE` &nbsp; `NUMBER(12,2)` → `NUMBER(15,2)`
- `CUTBLA.CB_NAME` &nbsp; `VARCHAR2(30)` → `VARCHAR2(50)`

Findings: CRITICAL 2, MEDIUM 2, INFO 2.

## Files to edit

### `examples/redefines/src/wvload.pco`

- **WV-CUST-NAME** (line 29, CRITICAL): `05 WV-CUST-NAME PIC X(30) DISPLAY` → `05 WV-CUST-NAME PIC X(50).`
- **WV-VALUE** (line 27, CRITICAL): `05 WV-VALUE PIC S9(10)V9(2) DISPLAY` → `05 WV-VALUE PIC S9(13)V9(2).`
- **WV-CUTBLA-KEY** — MEDIUM — WV-CUTBLA-KEY: record length grows 42 -> 62 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.
- **WV-CUTBLA-VIEW** — MEDIUM — WV-CUTBLA-VIEW: record length grows 50 -> 70 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.
  - overlays the same storage (REDEFINES): 01 WV-DATA PIC X(4096) (4096 bytes, still fits the new 70)

## Recompile only (rebuild, no source edit)

_None._

## DDL plan

```sql
ALTER TABLE CUTBLA MODIFY CB_VALUE NUMBER(15,2);
ALTER TABLE CUTBLA MODIFY CB_NAME VARCHAR2(50);
```
