# CSS_CUSTOMER widening — changes by file

_Generated 2026-09-10 17:24:41Z._

## Requested change

- `CSS_CUSTOMER.CUST_NAME` &nbsp; `VARCHAR2(30)` → `VARCHAR2(40)`
- `CSS_CUSTOMER.CUST_BALANCE` &nbsp; `NUMBER(11,2)` → `NUMBER(13,2)`

Findings: CRITICAL 25, HIGH 11, MEDIUM 9, INFO 2.

## Files to edit

### `examples/css/copybooks/CSACCT.cpy`

- **CS-ACCT-BILL-NAME** (line 10, CRITICAL): `05 CS-ACCT-BILL-NAME PIC X(30) DISPLAY` → `05 CS-ACCT-BILL-NAME PIC X(40).`
- **CS-ACCOUNT-REC** — MEDIUM — CS-ACCOUNT-REC: record length grows 63 -> 73 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/css/copybooks/CSCUST.cpy`

- **CS-CUST-BALANCE** (line 14, CRITICAL): `05 CS-CUST-BALANCE PIC S9(9)V99 COMP-3` → `05 CS-CUST-BALANCE PIC S9(11)V9(2) COMP-3.`
- **CS-CUST-NAME** (line 7, CRITICAL): `05 CS-CUST-NAME PIC X(30) DISPLAY` → `05 CS-CUST-NAME PIC X(40).`
- **CS-CUSTOMER-REC** — MEDIUM — CS-CUSTOMER-REC: record length grows 146 -> 156 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/css/copybooks/CSSVC.cpy`

- **CS-SVC-CUST-NAME** (line 8, CRITICAL): `05 CS-SVC-CUST-NAME PIC X(30) DISPLAY` → `05 CS-SVC-CUST-NAME PIC X(40).`
- **CS-SVC-TECH-NOTES** (line 11, HIGH): `05 CS-SVC-TECH-NOTES PIC X(60) DISPLAY` → `05 CS-SVC-TECH-NOTES PIC X(70).`
- **CS-SVC-ORDER-REC** — MEDIUM — CS-SVC-ORDER-REC: record length grows 109 -> 119 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/css/copybooks/CSWORK.cpy`

- **CS-RL-BALANCE** (line 11, CRITICAL): `05 CS-RL-BALANCE PIC ZZ,ZZZ,ZZ9.99- DISPLAY` → `05 CS-RL-BALANCE PIC ZZZZZ,ZZZ,ZZ9.99-.`
- **CS-RL-BALANCE** (line 11, CRITICAL): `05 CS-RL-BALANCE PIC ZZ,ZZZ,ZZ9.99- DISPLAY` → `05 CS-RL-BALANCE PIC ZZZZZZZ,ZZZ,ZZ9.99-.`
- **CS-RL-NAME** (line 7, CRITICAL): `05 CS-RL-NAME PIC X(30) DISPLAY` → `05 CS-RL-NAME PIC X(40).`
- **CS-SHORT-NAME** (line 20, CRITICAL): `01 CS-SHORT-NAME PIC X(20) DISPLAY` → `01 CS-SHORT-NAME PIC X(40).`
- **CS-CUST-KEY** (line 18, HIGH): `01 CS-CUST-KEY PIC X(40) DISPLAY` → `01 CS-CUST-KEY PIC X(60).`
- **CS-RL-FLAT** (line 15, HIGH): `01 CS-RL-FLAT PIC X(83) DISPLAY` → `01 CS-RL-FLAT REDEFINES CS-REPORT-LINE PIC X(93).`
- **CS-SEARCH-NAME** (line 17, HIGH): `01 CS-SEARCH-NAME PIC X(30) DISPLAY` → `01 CS-SEARCH-NAME PIC X(40).`
- **CS-REPORT-LINE** — MEDIUM — CS-REPORT-LINE: record length grows 83 -> 93 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/css/copybooks/CSXTRACT.cpy`

- **XT-BALANCE** (line 9, CRITICAL): `05 XT-BALANCE PIC S9(11)V99 COMP-3` → `05 XT-BALANCE PIC S9(13)V9(2) COMP-3.`
- **XT-CUST-NAME** (line 7, CRITICAL): `05 XT-CUST-NAME PIC X(30) DISPLAY` → `05 XT-CUST-NAME PIC X(40).`
- **CS-XTRACT-REC** — MEDIUM — CS-XTRACT-REC: record length grows 87 -> 97 bytes: Rebuild the record layout, then recompile every program that copies this group and reload any file written with the old length.

### `examples/css/src/csacctupd.pco`

- **WS-AUDIT-TEXT** (line 15, CRITICAL): `01 WS-AUDIT-TEXT PIC X(80) DISPLAY` → `01 WS-AUDIT-TEXT PIC X(90).`

### `examples/css/src/csbillcyc.pco`

- **EXTRACT-RECORD** (line 12, CRITICAL): `01 EXTRACT-RECORD PIC X(80) DISPLAY` → `01 EXTRACT-RECORD PIC X(97).`
- **WS-NEW-BALANCE** (line 22, CRITICAL): `01 WS-NEW-BALANCE PIC S9(11)V99 COMP-3` → `01 WS-NEW-BALANCE PIC S9(13)V9(2) COMP-3.`

### `examples/css/src/csfmtnam.pco`

- **LK-DISPLAY-KEY** (line 16, CRITICAL): `01 LK-DISPLAY-KEY PIC X(40) DISPLAY` → `01 LK-DISPLAY-KEY PIC X(60).`
- **WS-FIRST-NAME** (line 12, CRITICAL): `01 WS-FIRST-NAME PIC X(20) DISPLAY` → `01 WS-FIRST-NAME PIC X(40).`
- **WS-LAST-NAME** (line 11, CRITICAL): `01 WS-LAST-NAME PIC X(20) DISPLAY` → `01 WS-LAST-NAME PIC X(40).`
- **WS-WORK-NAME** (line 10, CRITICAL): `01 WS-WORK-NAME PIC X(30) DISPLAY` → `01 WS-WORK-NAME PIC X(40).`
- **LK-RAW-NAME** (line 15, HIGH): `01 LK-RAW-NAME PIC X(30) DISPLAY` → `01 LK-RAW-NAME PIC X(40).`

### `examples/css/src/csnotes.pco`

- **LK-TECH-NOTES** (line 13, CRITICAL): `01 LK-TECH-NOTES PIC X(60) DISPLAY` → `01 LK-TECH-NOTES PIC X(70).`
- **WS-NOTE-HEADER** (line 10, CRITICAL): `01 WS-NOTE-HEADER PIC X(30) DISPLAY` → `01 WS-NOTE-HEADER PIC X(40).`
- **LK-CUST-NAME** (line 12, HIGH): `01 LK-CUST-NAME PIC X(30) DISPLAY` → `01 LK-CUST-NAME PIC X(40).`

## Recompile only (rebuild, no source edit)

- **CSCUSTRPT** — includes CSCUST.cpy, CSWORK.cpy
- **CSPURGE** — includes CSCUST.cpy
- **CSSVCORD** — includes CSCUST.cpy, CSSVC.cpy

## DDL plan

```sql
ALTER TABLE CSS_CUSTOMER MODIFY CUST_NAME VARCHAR2(40);
ALTER TABLE CSS_CUSTOMER MODIFY CUST_BALANCE NUMBER(13,2);
ALTER TABLE CSS_ACCOUNT MODIFY ACCT_BILL_NAME VARCHAR2(40);
ALTER TABLE CSS_ACCT_AUDIT MODIFY AUDIT_BILL_NAME VARCHAR2(40);
ALTER TABLE CSS_ACCT_AUDIT MODIFY AUDIT_TEXT VARCHAR2(90);
ALTER TABLE CSS_SVC_ORDER MODIFY SVC_CUST_NAME VARCHAR2(40);
```
