# CSS example — verification

Run:

```bash
python -m cobol_impact_analyzer --spec examples/css/change_spec.json --out examples/output
```

Change: `CSS_CUSTOMER.CUST_NAME` `VARCHAR2(30) → VARCHAR2(40)` and
`CSS_CUSTOMER.CUST_BALANCE` `NUMBER(11,2) → NUMBER(13,2)`.

Result: 8 programs scanned, 8 affected, 3 recompile-only, 0 coverage warnings,
191 graph nodes / 195 edges. Findings: CRITICAL 25, HIGH 11, MEDIUM 9, INFO 2.

## What each chain proves

| Mechanism | Where | Expected | Seen |
| --- | --- | --- | --- |
| Cursor `FETCH INTO` | `CSCUSTINQ`, `CSBILLCYC` | `CS-CUST-NAME`, `CS-CUST-BALANCE` CRITICAL at the fetch | yes |
| `SELECT INTO` | `CSACCTUPD`, `CSSVCORD` | same, at the select | yes |
| `MOVE` into a copybook field | `CS-RL-NAME`, `CS-ACCT-BILL-NAME`, `CS-SVC-CUST-NAME`, `XT-CUST-NAME` | CRITICAL, edit lands in the copybook | yes |
| `COMPUTE` grows by the delta | `CSBILLCYC` `WS-NEW-BALANCE` | `S9(11)V9(2) → S9(13)V9(2)` CRITICAL | yes |
| Edited picture | `CS-RL-BALANCE` `ZZ,ZZZ,ZZ9.99-` | widened, human-review picture suggested | yes |
| `UPDATE`/`INSERT` bind → other column | `CSS_ACCOUNT.ACCT_BILL_NAME`, `CSS_ACCT_AUDIT.AUDIT_BILL_NAME`, `CSS_SVC_ORDER.SVC_CUST_NAME` | HIGH + an `ALTER` in the DDL plan | yes |
| `WRITE ... FROM` | `CSBILLCYC` `EXTRACT-RECORD` | CRITICAL, record grows | yes |
| Group / record length | `CS-CUSTOMER-REC`, `CS-ACCOUNT-REC`, `CS-SVC-ORDER-REC`, `CS-REPORT-LINE`, `CS-XTRACT-REC` | MEDIUM record-layout | yes |
| `REDEFINES` of the print line | `CS-RL-FLAT` | HIGH (non-truncating, shares storage) | yes |
| Reference modification | `CSCUSTINQ` `MOVE CS-CUST-NAME (1:20)` | HIGH note folded onto `CS-CUST-NAME` | yes |
| `CALL ... USING` into a subprogram | `CSCUSTINQ → CSFMTNAM`, `CSSVCORD → CSNOTES` | arg field HIGH (call-arg, non-truncating), then CRITICAL on the internal `MOVE`/`UNSTRING` | yes |
| `CALL` output argument back to the caller | `LK-DISPLAY-KEY → CS-CUST-KEY`, `LK-TECH-NOTES → CS-SVC-TECH-NOTES` | HIGH in the caller | yes |
| Host variable in a `WHERE` predicate | `CSCUSTINQ` `CS-SEARCH-NAME` | HIGH (not CRITICAL — a predicate does not truncate) | yes |
| Recompile-only classification | `CSCUSTRPT`, `CSPURGE`, `CSSVCORD` | in the rebuild list, nothing to edit in their own source | yes |
| Both `CALL`ees are in the scan | — | no "unresolved CALL" warning | yes (0 warnings) |

## Conservative-by-design behaviour (matches the `CUSTOMER` example)

- `STRING`/`UNSTRING` destinations grow by the widened operand's delta even when
  they had slack — so `WS-AUDIT-TEXT` `X(80) → X(90)` and, through it,
  `CSS_ACCT_AUDIT.AUDIT_TEXT` gets an `ALTER`. The original picture is shown next
  to the suggestion; treat the width as an upper bound.
- `MOVE src(1:20) TO dst` propagates `src`'s full new width to `dst` rather than
  the reference-modification length. The hard-coded `(1:20)` is itself flagged
  HIGH on `src`; the assumption is the intent was "the whole name".
- `CS-RL-BALANCE` appears twice in *Changes by module* — once for the 11-digit
  path (direct fetch) and once for the 13-digit path (after `COMPUTE`). Apply the
  wider of the two.
