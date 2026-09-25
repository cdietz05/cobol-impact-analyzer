# CUSTOMER widening — changes by file

_Generated 2026-09-25 19:56:25Z._

## Requested change

- `CUSTOMER.CUST_NAME` &nbsp; `VARCHAR2(30)` → `VARCHAR2(60)`
- `CUSTOMER.CUST_BALANCE` &nbsp; `NUMBER(11,2)` → `NUMBER(13,2)`

Findings: CRITICAL 10, HIGH 8, MEDIUM 4, INFO 2.

## Files to edit

### `examples/copybooks/CUSTOMER.cpy`

- **CUST-BALANCE** (line 11, CRITICAL): `05 CUST-BALANCE PIC S9(9)V99 COMP-3` → `05 CUST-BALANCE PIC S9(11)V9(2) COMP-3.`
  - copybook: CUSTOMER.cpy line 11 is included by 6 program(s) - CUSTARCH, CUSTLIST, CUSTPURG, CUSTTBL, CUSTUPD, ORDENTRY. One edit to the copybook covers all of them; each is rebuilt.
- **CUST-NAME** (line 6, CRITICAL): `05 CUST-NAME PIC X(30) DISPLAY` → `05 CUST-NAME PIC X(60).`
  - reference-modification: the offset/length is hard-coded and will not follow the new width.
  - copybook: CUSTOMER.cpy line 6 is included by 6 program(s) - CUSTARCH, CUSTLIST, CUSTPURG, CUSTTBL, CUSTUPD, ORDENTRY. One edit to the copybook covers all of them; each is rebuilt.
- **CUSTOMER-REC** — MEDIUM — CUSTOMER-REC: record length grows 114 -> 145 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.
  - copybook: CUSTOMER.cpy line 4 is included by 6 program(s) - CUSTARCH, CUSTLIST, CUSTPURG, CUSTTBL, CUSTUPD, ORDENTRY. One edit to the copybook covers all of them; each is rebuilt.

### `examples/copybooks/ORDERREC.cpy`

- **ORD-SHIP-NAME** (line 7, CRITICAL): `05 ORD-SHIP-NAME PIC X(30) DISPLAY` → `05 ORD-SHIP-NAME PIC X(60).`
- **ORDER-REC** — MEDIUM — ORDER-REC: record length grows 47 -> 77 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/copybooks/WSCOMMON.cpy`

- **RL-BALANCE** (line 11, CRITICAL): `05 RL-BALANCE PIC ZZ,ZZZ,ZZ9.99- DISPLAY` → `05 RL-BALANCE PIC ZZ,ZZZ,ZZZ,ZZ9.99-.`
- **RL-CUST-NAME** (line 7, CRITICAL): `05 RL-CUST-NAME PIC X(30) DISPLAY` → `05 RL-CUST-NAME PIC X(60).`
- **WS-NAME-KEY** (line 17, CRITICAL): `01 WS-NAME-KEY PIC X(30) DISPLAY` → `01 WS-NAME-KEY PIC X(60).`
- **WS-REPORT-FLAT** (line 15, HIGH): `01 WS-REPORT-FLAT PIC X(83) DISPLAY` → `01 WS-REPORT-FLAT REDEFINES WS-REPORT-LINE PIC X(117).`
  - overlays the same storage (REDEFINES): 01 WS-REPORT-LINE (83 bytes, smaller than the new 117 - check it)
- **WS-SEARCH-NAME** (line 18, HIGH): `01 WS-SEARCH-NAME PIC X(30) DISPLAY` → `01 WS-SEARCH-NAME PIC X(60).`
- **WS-REPORT-LINE** — MEDIUM — WS-REPORT-LINE: record length grows 83 -> 117 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.
  - overlays the same storage (REDEFINES): 01 WS-REPORT-FLAT REDEFINES WS-REPORT-LINE PIC X(83) (83 bytes, smaller than the new 117 - check it)
  - display: report or log column alignment shifts.

### `examples/src/cust_archive.pco`

- **WS-ARCH-NAME** (line 14, CRITICAL): `01 WS-ARCH-NAME PIC X(30) DISPLAY` → `01 WS-ARCH-NAME PIC X(60).`

### `examples/src/cust_table.pco`

- **WS-BAL-ENTRY** (line 17, CRITICAL): `05 WS-BAL-ENTRY PIC S9(9)V99 COMP-3` → `05 WS-BAL-ENTRY OCCURS 100 PIC S9(11)V9(2) COMP-3.`
- **WS-BAL-TABLE** — MEDIUM — WS-BAL-TABLE: record length grows 600 -> 700 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/src/fmtname.pco`

- **WS-WORK-NAME** (line 9, CRITICAL): `01 WS-WORK-NAME PIC X(30) DISPLAY` → `01 WS-WORK-NAME PIC X(60).`
  - inspect: INSPECT scans the whole field including trailing spaces, so counts change.
  - literal-comparison: the literal is padded to the field width, so a wider field changes the comparison.
- **LK-CUST-NAME** (line 12, HIGH): `01 LK-CUST-NAME PIC X(30) DISPLAY` → `01 LK-CUST-NAME PIC X(60).`

### `examples/src/order_entry.pco`

- **WS-SHIP-LABEL** (line 13, CRITICAL): `01 WS-SHIP-LABEL PIC X(30) DISPLAY` → `01 WS-SHIP-LABEL PIC X(60).`
- **WS-INVOICE-KEY** (line 14, HIGH): `01 WS-INVOICE-KEY PIC X(40) DISPLAY` → `01 WS-INVOICE-KEY PIC X(60).`

### `examples/src/io_custname.pco`

- **LK-IO-NAME** (line 15, HIGH): `01 LK-IO-NAME PIC X(30) DISPLAY` → `01 LK-IO-NAME PIC X(60).`

## Recompile only (rebuild, no source edit)

- **CUSTLIST** — includes CUSTOMER.cpy
- **CUSTPURG** — includes CUSTOMER.cpy

## DDL plan

```sql
ALTER TABLE CUSTOMER MODIFY CUST_NAME VARCHAR2(60);
ALTER TABLE CUSTOMER MODIFY CUST_BALANCE NUMBER(13,2);
ALTER TABLE CUST_ARCHIVE MODIFY ARCH_NAME VARCHAR2(60);
ALTER TABLE ORDER_HISTORY MODIFY CUST_NAME_SNAP VARCHAR2(60);
ALTER TABLE ORDER_SHIP MODIFY SHIP_NAME VARCHAR2(60);
```
