# CSS example — verification

Run:

```bash
python -m cobol_impact_analyzer --spec examples/css/change_spec.json --out examples/output
```

Change: `CSS_CUSTOMER.CUST_NAME` `VARCHAR2(30) → VARCHAR2(40)` and
`CSS_CUSTOMER.CUST_BALANCE` `NUMBER(11,2) → NUMBER(13,2)`.

Result: 8 programs scanned, 8 affected, 3 recompile-only, 0 coverage warnings,
219 graph nodes / 453 edges. Findings: CRITICAL 23, HIGH 11, MEDIUM 12, LOW 11, INFO 2.

## What each chain proves

| Mechanism | Where | Expected | Seen |
| --- | --- | --- | --- |
| Cursor `FETCH INTO` | `CSCUSTINQ`, `CSBILLCYC` | `CS-CUST-NAME`, `CS-CUST-BALANCE` CRITICAL at the fetch | yes |
| `SELECT INTO` | `CSACCTUPD`, `CSSVCORD` | same, at the select | yes |
| `MOVE` into a copybook field | `CS-RL-NAME`, `CS-ACCT-BILL-NAME`, `CS-SVC-CUST-NAME`, `XT-CUST-NAME` | CRITICAL, edit lands in the copybook | yes |
| `COMPUTE` grows by the delta | `CSBILLCYC` `WS-NEW-BALANCE` | `S9(11)V9(2) → S9(13)V9(2)` CRITICAL | yes |
| Edited picture | `CS-RL-BALANCE` `ZZ,ZZZ,ZZ9.99-` | widened to `Z,ZZZ,ZZZ,ZZZ,ZZ9.99-`, commas regrouped | yes |
| `UPDATE`/`INSERT` bind → other column | `CSS_ACCOUNT.ACCT_BILL_NAME`, `CSS_ACCT_AUDIT.AUDIT_BILL_NAME`, `CSS_SVC_ORDER.SVC_CUST_NAME` | HIGH + an `ALTER` in the DDL plan | yes |
| `WRITE ... FROM` | `CSBILLCYC` `EXTRACT-RECORD` | CRITICAL, record grows | yes |
| Group / record length | `CS-CUSTOMER-REC`, `CS-ACCOUNT-REC`, `CS-SVC-ORDER-REC`, `CS-REPORT-LINE`, `CS-XTRACT-REC` | MEDIUM record-layout, in bytes, every widened member added up — `CS-CUSTOMER-REC` `146 -> 157` (10 for the name, 1 for the packed balance) in every includer | yes |
| `REDEFINES` of the print line | `CS-RL-FLAT` | HIGH (non-truncating, shares storage) | yes |
| Reference modification | `CSCUSTINQ` `MOVE CS-CUST-NAME (1:20)` | HIGH note folded onto `CS-CUST-NAME`; `CS-SHORT-NAME` is not widened, since 20 characters arrive however wide the name gets | yes |
| `STRING` sums its sources | `CSNOTES` `LK-TECH-NOTES`; `CSACCTUPD` `WS-AUDIT-TEXT` | `'CUST: '` + name + `' - NEW SERVICE ORDER'` is 6 + 40 + 20 = `X(66)`; `'REBILL '` + name is 47, fits `X(80)`, not reported | yes |
| `CALL ... USING` into a subprogram | `CSCUSTINQ → CSFMTNAM`, `CSSVCORD → CSNOTES` | arg field HIGH (call-arg, non-truncating), then CRITICAL on the internal `MOVE`/`UNSTRING` | yes |
| `CALL` output argument back to the caller | `LK-DISPLAY-KEY → CS-CUST-KEY`, `LK-TECH-NOTES → CS-SVC-TECH-NOTES` | HIGH in the caller | yes |
| Host variable in a `WHERE` predicate | `CSCUSTINQ` `CS-SEARCH-NAME` | HIGH (not CRITICAL — a predicate does not truncate) | yes |
| Recompile-only classification | `CSACCTUPD`, `CSPURGE`, `CSSVCORD` | in the rebuild list, nothing to edit in their own source | yes |
| Copybook field widened by another program | `CS-CUST-NAME` in `CSCUSTRPT`, `CSPURGE` | LOW `copybook-field` — the copybook is edited for other programs; rebuild only | yes |
| Statement assuming the old width | `CSCUSTRPT` `DISPLAY CS-RL-FLAT` | the print line it displays grows `83 -> 100`, so `CSCUSTRPT` is a source change, not recompile-only | yes |
| Both `CALL`ees are in the scan | — | no "unresolved CALL" warning | yes (0 warnings) |

## Conservative-by-design behaviour

- An `UNSTRING` piece is assumed to be as long as the whole widened source,
  because the delimiter could fall anywhere. `CSFMTNAM` splits `WS-WORK-NAME`
  into `WS-LAST-NAME` and `WS-FIRST-NAME`, so each is taken as `X(40)`, and the
  `STRING` that joins them with a space is taken as 40 + 1 + 40 = `X(81)` for
  `LK-DISPLAY-KEY` and, through the `CALL`, `CS-CUST-KEY`. The real ceiling is
  41, because the two pieces come from one 40-character name. Treat the width
  as an upper bound.
