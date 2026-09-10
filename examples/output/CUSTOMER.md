# CUSTOMER widening — changes by file

_Generated 2026-09-10 16:15:21Z._

## Requested change

- `CUSTOMER.CUST_NAME` &nbsp; `VARCHAR2(30)` → `VARCHAR2(60)`
- `CUSTOMER.CUST_BALANCE` &nbsp; `NUMBER(11,2)` → `NUMBER(13,2)`

Findings: CRITICAL 16, HIGH 10, MEDIUM 8, INFO 2.

## Files to edit

### `examples/copybooks/CUSTOMER.cpy`

- **CUST-BALANCE** (line 11, CRITICAL): `05 CUST-BALANCE PIC S9(9)V99 COMP-3` → `05 CUST-BALANCE PIC S9(11)V9(2) COMP-3.`
- **CUST-NAME** (line 6, CRITICAL): `05 CUST-NAME PIC X(30) DISPLAY` → `05 CUST-NAME PIC X(60).`
- **CUSTOMER-REC** — MEDIUM — CUSTOMER-REC: record length grows 114 -> 144 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/copybooks/ORDERREC.cpy`

- **ORD-SHIP-NAME** (line 7, CRITICAL): `05 ORD-SHIP-NAME PIC X(30) DISPLAY` → `05 ORD-SHIP-NAME PIC X(60).`
- **ORDER-REC** — MEDIUM — ORDER-REC: record length grows 47 -> 77 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/copybooks/WSCOMMON.cpy`

- **RL-BALANCE** (line 11, CRITICAL): `05 RL-BALANCE PIC ZZ,ZZZ,ZZ9.99- DISPLAY` → `05 RL-BALANCE PIC ZZZZZ,ZZZ,ZZ9.99-.`
- **RL-CUST-NAME** (line 7, CRITICAL): `05 RL-CUST-NAME PIC X(30) DISPLAY` → `05 RL-CUST-NAME PIC X(60).`
- **WS-NAME-KEY** (line 17, CRITICAL): `01 WS-NAME-KEY PIC X(30) DISPLAY` → `01 WS-NAME-KEY PIC X(60).`
- **WS-FULL-ADDRESS** (line 19, HIGH): `01 WS-FULL-ADDRESS PIC X(70) DISPLAY` → `01 WS-FULL-ADDRESS PIC X(100).`
- **WS-REPORT-FLAT** (line 15, HIGH): `01 WS-REPORT-FLAT PIC X(83) DISPLAY` → `01 WS-REPORT-FLAT REDEFINES WS-REPORT-LINE PIC X(113).`
- **WS-SEARCH-NAME** (line 18, HIGH): `01 WS-SEARCH-NAME PIC X(30) DISPLAY` → `01 WS-SEARCH-NAME PIC X(60).`
- **WS-REPORT-LINE** — MEDIUM — WS-REPORT-LINE: record length grows 83 -> 113 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/src/cust_archive.pco`

- **WS-ARCH-NAME** (line 14, CRITICAL): `01 WS-ARCH-NAME PIC X(30) DISPLAY` → `01 WS-ARCH-NAME PIC X(60).`

### `examples/src/cust_table.pco`

- **WS-BAL-ENTRY** (line 17, CRITICAL): `05 WS-BAL-ENTRY PIC S9(9)V99 COMP-3` → `05 WS-BAL-ENTRY OCCURS 100 PIC S9(11)V9(2) COMP-3.`
- **WS-BAL-TABLE** — MEDIUM — WS-BAL-TABLE: record length grows 600 -> 602 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/src/cust_update.pco`

- **WS-SNAPSHOT-NAME** (line 12, CRITICAL): `01 WS-SNAPSHOT-NAME PIC X(30) DISPLAY` → `01 WS-SNAPSHOT-NAME PIC X(60).`

### `examples/src/fmtname.pco`

- **LK-FULL-ADDRESS** (line 13, CRITICAL): `01 LK-FULL-ADDRESS PIC X(70) DISPLAY` → `01 LK-FULL-ADDRESS PIC X(100).`
- **WS-WORK-NAME** (line 9, CRITICAL): `01 WS-WORK-NAME PIC X(30) DISPLAY` → `01 WS-WORK-NAME PIC X(60).`
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
ALTER TABLE ORDER_AUDIT MODIFY AUDIT_CUST_NAME VARCHAR2(60);
ALTER TABLE ORDER_HISTORY MODIFY CUST_NAME_SNAP VARCHAR2(60);
ALTER TABLE ORDER_SHIP MODIFY SHIP_NAME VARCHAR2(60);
```
