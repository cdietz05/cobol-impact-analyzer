# cobol-impact-analyzer

Trace the blast radius of a SQL column widening through Pro\*COBOL sources.

You give it a table, the columns you want to grow, the copybook that declares
the host variables, and your source directories. It tells you every COBOL field
that has to grow, what to grow it to, every *other* database column that must be
altered because the widened value flows into it, and every place the change will
quietly break something (reference modification, literal comparisons, record
layouts, `CALL` interfaces).

Pure standard library. No pip install of anything. Runs on Python 3.12+.

---

## Why

Widening `CUSTOMER.CUST_NAME` from `VARCHAR2(30)` to `VARCHAR2(60)` is a one-line
DDL change and a multi-week outage if you get it wrong. The value does not stop
at the host variable: it is moved into report lines, concatenated into keys,
passed to subprograms through `LINKAGE`, and written back into three other
tables that nobody remembers. COBOL truncates silently — no error, no warning,
just wrong data.

This tool builds the data-flow graph and pushes the new width along it until
nothing else grows.

---

## Install

```bash
git clone https://github.com/cdietz05/cobol-impact-analyzer.git
cd cobol-impact-analyzer
```

That is enough to run it:

```bash
python -m cobol_impact_analyzer --help
```

Optionally install it so `cobol-impact` is on your PATH:

```bash
pip install -e .
```

---

## Quick start

Run it against the bundled example:

```bash
python -m cobol_impact_analyzer --spec examples/change_spec.json --out examples/output
```

You get `CUSTOMER.json`, `CUSTOMER.csv`, `CUSTOMER.html` and `CUSTOMER.md`
(a per-file change list) in that directory. The generated set for the bundled
example is committed under [`examples/output/`](examples/output/) so you can read
it without running anything.

A second, larger example under [`examples/css/`](examples/css/) models a 1990s
telecom **Customer Service System** — customer master, accounts, service orders,
a nightly billing cycle, a flat warehouse extract, and two called subprograms —
across eight `.pco` sources and five copybooks. It exercises cursor `FETCH`,
`SELECT INTO`, `INSERT`/`UPDATE` binds, `COMPUTE`, `WRITE FROM`, `REDEFINES`,
and `CALL ... USING` in both directions:

```bash
python -m cobol_impact_analyzer --spec examples/css/change_spec.json --out examples/output
```

writes `CSS_CUSTOMER.{json,csv,html,md}` — also committed under `examples/output/`.

The stdout report has three sections — the base value, the trace, and the edits
per file:

```
REQUESTED CHANGES
  CUSTOMER.CUST_NAME                       VARCHAR2(30) -> VARCHAR2(60)
  CUSTOMER.CUST_BALANCE                    NUMBER(11,2) -> NUMBER(13,2)

FLOW  (where the value moves; [SEVERITY] marks a field that will not hold it)
  CUSTOMER.CUST_NAME   VARCHAR2(30) -> VARCHAR2(60)
    CUST-NAME  X(30) -> X(60)  [CRITICAL]  CUSTUPD  examples/src/cust_update.pco:26
      WS-SNAPSHOT-NAME  X(30) -> X(60)  [CRITICAL]  CUSTUPD  examples/src/cust_update.pco:46
        ORDER_AUDIT.AUDIT_CUST_NAME  X(30) -> X(60)  [HIGH]  CUSTUPD  .../cust_update.pco:54

CHANGES BY MODULE  (9 file(s) to edit)
  examples/copybooks/CUSTOMER.cpy
      CUST-NAME                05 CUST-NAME PIC X(30) DISPLAY
                                 -> 05 CUST-NAME PIC X(60).   (line 6)
```

In the HTML the severity tiles at the top link straight to that severity's
section.

Or skip the spec file entirely:

```bash
python -m cobol_impact_analyzer \
  --table CUSTOMER \
  --column CUST_NAME --from 'VARCHAR2(30)' --to 'VARCHAR2(60)' \
  --source /code/pco --copybook /code/copybooks \
  --html impact.html
```

---

## The change spec

A JSON file. The flat form covers one table:

```json
{
  "table": "CUSTOMER",
  "source_paths": ["src"],
  "copybook_paths": ["copybooks"],
  "source_patterns": ["*.pco"],
  "columns": [
    { "name": "CUST_NAME",    "from": "VARCHAR2(30)", "to": "VARCHAR2(60)" },
    { "name": "CUST_BALANCE", "from": "NUMBER(11,2)", "to": "NUMBER(13,2)" }
  ]
}
```

The general form spans tables:

```json
{
  "source_paths": ["/code/pco"],
  "copybook_paths": ["/code/copybooks"],
  "changes": [
    { "table": "CUSTOMER", "column": "CUST_NAME", "from": "VARCHAR2(30)", "to": "VARCHAR2(60)" },
    { "table": "ORDERS",   "column": "ORDER_NBR", "from": "NUMBER(9)",    "to": "NUMBER(12)" }
  ]
}
```

Relative paths resolve against the spec file's own directory.

| Key | Meaning |
| --- | --- |
| `table` + `columns` | single-table shorthand |
| `changes` | one entry per column, across any number of tables |
| `source_paths` | directories (or single files) of COBOL/Pro\*COBOL source |
| `copybook_paths` | directories searched for `COPY` and `EXEC SQL INCLUDE` |
| `source_patterns` | filename globs, default `["*.pco"]` |
| `source_format` | `fixed` or `free`; omit to auto-detect per file |
| `max_depth` | stop after N hops; `0` (default) means unlimited |
| `global_variable_scope` | treat same-named inline fields in different programs as one node |

---

## What it reads out of a copybook — and how it links to a column

The copybook supplies **declarations only**. For each entry it records the level
number, name, `PIC`, `USAGE`, `OCCURS`, `REDEFINES`, `VALUE` and any `88`
condition names, then derives two things: the **capacity** (character positions,
or integer and decimal digits) and the **byte size**, which depends on usage —
`PIC S9(9) COMP-3` is 11 digits in 6 bytes, not 11. Group items get the sum of
their children, which is the record length that matters when the layout moves.

Nothing in the copybook is matched against a column name. **The link is made
entirely by the embedded SQL.** When a program says:

```cobol
EXEC SQL
    SELECT CUST_NAME INTO :CUST-NAME FROM CUSTOMER
END-EXEC
```

the tool binds `CUSTOMER.CUST_NAME` to whichever field is declared as
`CUST-NAME`. That is why the SQL underscore / COBOL hyphen convention causes no
trouble: the two spellings never have to agree, because the statement already
says which is which. (If a host variable name misses entirely, one
hyphen/underscore-insensitive retry runs and warns loudly rather than silently
binding the wrong field.)

The consequence is worth stating plainly: **a copybook on its own tells the tool
nothing.** A field only enters the graph when some `.pco` names it as a host
variable, or moves data into it from one that is. A table copybook whose fields
are never referenced individually in SQL produces no findings — which is exactly
the case in the next paragraph.

### Cursors

`DECLARE ... CURSOR FOR SELECT`, `OPEN`, `FETCH ... INTO` and `CLOSE` are all
handled. The cursor's select list is remembered at declaration and applied
positionally to every `FETCH INTO` against it, so this traces exactly as if the
columns were named at the fetch:

```cobol
EXEC SQL
    DECLARE CUST_CUR CURSOR FOR
    SELECT C.CUST_ID, C.CUST_NAME FROM CUSTOMER C
END-EXEC
...
EXEC SQL FETCH CUST_CUR INTO :CUST-ID, :CUST-NAME END-EXEC
```

`CUSTOMER.CUST_NAME` binds to `CUST-NAME` through the declaration. Table aliases
(`C.CUST_NAME`), `WHERE` predicates on the declaration, `ORDER BY` and
`FOR UPDATE` tails are all handled.

**Cursor names are scoped to one program.** Shops reuse `C1` and `CUR1` across
hundreds of programs, so a shared namespace would let one program's `DECLARE`
supply the mapping for another program's `FETCH` — inventing findings, or
mapping a fetch to the wrong table. When a program fetches a cursor it does not
declare (a genuine pattern when the `DECLARE` lives in a copybook), the mapping
is still offered, but a warning names the program it was borrowed from so you
can confirm they are the same cursor. A cursor declared nowhere in the scan is
reported and its columns are left unmapped rather than guessed.

Not handled: `FETCH ... USING DESCRIPTOR` and anything else built through
`PREPARE`/`EXECUTE`, which is dynamic by definition.

### The group-host-variable blind spot

Some shops bind a whole record at once:

```cobol
EXEC SQL SELECT * INTO :CU01TB01-REC FROM CUSTOMER END-EXEC
```

There is no column-to-field mapping here to recover — it needs the table DDL,
which this tool does not read. You get a `SELECT * cannot be mapped to columns`
warning and the columns are not traced. If your programs are written this way,
the impact trace starts at the group rather than the individual column, and you
should widen the scan manually. Statements that name columns explicitly are the
ones that trace cleanly.

---

## What it understands

**Declarations.** Fixed-format card image and free format, `COPY` with
`REPLACING`, `EXEC SQL INCLUDE`, level hierarchies, `OCCURS`, `REDEFINES`,
`88` condition names, `VALUE` clauses, `PIC` with `DISPLAY`, `COMP`, `COMP-3`,
`COMP-4/5`, `COMP-1/2`, `NATIONAL`, edited pictures like `ZZ,ZZZ,ZZ9.99-`, and
Pro\*COBOL `VARYING`.

Format is auto-detected per file, and a `--format` override now applies to the
copybooks a file pulls in as well, not just the file itself. Two card-image
quirks are tolerated rather than silently swallowed: a level number typed left
of column 8 (`01   WV-DATA  PIC X.` starting in column 1) is read as free format
for that line even under `--format fixed`, and a `PICTURE` pushed past column 72
into the identification area is kept when the code area ends mid-clause, instead
of being cut off (which used to drop the whole declaration).

When a record grows, every layout that overlays the same bytes through a
`REDEFINES` is named as a note on the finding, with its size and whether it
still fits - so a wide flat buffer that happens to be large enough is called
out instead of passing silently.

**Embedded SQL.** `SELECT ... INTO`, `DECLARE CURSOR` + `FETCH INTO` (the cursor
select list is carried to the fetch), `INSERT` with an explicit column list,
`INSERT ... SELECT`, `UPDATE ... SET`, `DELETE`, `WHERE` predicates, indicator
variables (`:VAR:IND`), table aliases, and `OPEN ... USING`.

**Procedure division.** `MOVE` (including `CORRESPONDING` and reference
modification), `STRING`, `UNSTRING`, `COMPUTE`, `ADD`/`SUBTRACT`/`MULTIPLY`/
`DIVIDE` with and without `GIVING`, `CALL ... USING` matched positionally
against the callee's `PROCEDURE DIVISION USING`, `WRITE ... FROM`,
`READ ... INTO`, relational conditions, `INSPECT`, `INITIALIZE`, `DISPLAY`.

**Storage relationships.** Growing a field grows its parent group, which changes
the record length, which forces every `REDEFINES` of that group to change too.

---

## How the propagation works

1. Every column and every data item becomes a **node** holding its current
   capacity — character positions for text, integer/decimal digits for numerics.
2. Every bind, fetch, move, concatenation, arithmetic result and call argument
   becomes a directed **edge**.
3. The changed columns are seeded with their **new** capacity, which is pushed
   along the edges until no node grows any further.
4. Any node whose current capacity cannot cover its required capacity becomes a
   **finding**, reported with the path that reached it.

Edges are not all the same. A `MOVE` requires the destination to hold the whole
source. A `STRING` or `COMPUTE` only requires the destination to grow by the
*delta*, because it was already sized for the other operands. Group and
`REDEFINES` edges grow by the byte delta, not the character delta — which is why
a `COMP-3` field widening by two digits grows its record by one byte, not two.

---

## Severities

| Severity | Meaning |
| --- | --- |
| `CRITICAL` | Silent data loss. A wider value lands in a field too small for it through a **truncating** edge — `SELECT`/`FETCH INTO`, `MOVE`, `STRING`/`UNSTRING`, `COMPUTE`, arithmetic, `WRITE FROM`, `READ INTO`, reference modification. COBOL drops the overflow with no error: trailing characters for text, high-order digits for numbers. |
| `HIGH` | Wrong, but it surfaces. Another database column receives the value and needs its own `ALTER` (`ORA-01401`/`ORA-12899`); or a field is undersized on a **non-truncating** path (`CALL` argument, host variable in a `WHERE`, `REDEFINES` over shared storage); or a hard-coded reference modification no longer spans the field. |
| `MEDIUM` | Needs a human decision. A group/record length grew (every file, queue, `CALL` interface or `REDEFINES` on the old length must be rebuilt); a padded literal comparison or `INSPECT` shifts; a `VALUE` clause may be stale; or a column could not be traced at all (dynamic SQL, `SELECT *`, cursor in an unscanned copybook). |
| `LOW` | Cosmetic or informational. `DISPLAY` alignment, `INITIALIZE`, `SET`. |
| `INFO` | The change you asked for, echoed back with its DDL. The root of every trace. |

A field that trips several of these keeps one finding and takes the worst
severity. The same table, one line per severity, is in the text report and
behind the **What the severities mean** disclosure in the HTML page.

Use `--fail-on HIGH` in a pipeline to exit non-zero when anything at HIGH or
worse is found.

---

## Watching a long run

The report is only printed once the whole analysis finishes, so a large scan
would otherwise sit silent for minutes. Progress goes to **stderr**, on by
default, and adapts to where it is pointed — a terminal gets one line rewritten
in place, a log file gets a line every couple of seconds:

```
[00:00] discovering sources matching *.pco, *.PCO under /code/pco
[00:03] parsing 1847 source file(s)
[00:05] indexing copybooks under /code/copylib
[00:07] indexed 2310 copybook name(s)
[00:41] 1847/1847  .../code/pco/billing/bl9920.pco
[00:41] building the data-flow graph
[00:42] graph built: 48192 nodes, 91043 edges
[00:43] analysis complete: 214 finding(s)
```

Because stdout carries only the report, `... > impact.txt` still gives you a
clean file while progress stays on your terminal. `--no-progress` turns it off.

Notice the copybook index is built *after* parsing starts — it is lazy, and
scoped to plausible copybook extensions rather than every file under the search
path. If your shop uses an extension outside
`.cpy .cbl .cob .inc .copy .cpb .cbk .src` (or extensionless members), add it
with `--copybook-ext`. A name that misses the filtered index triggers one
unfiltered fallback scan rather than failing, so an unusual extension still
resolves — you just pay for the walk once.

---

## Output

- **stdout** — the base value, the flow trace, then the edits grouped by file.
  `--verbose` adds the reasoning behind each finding under its flow line.
- **`--json FILE`** — machine readable; `--include-graph` embeds the full node
  and edge list for your own tooling.
- **`--csv FILE`** — one row per finding, for a spreadsheet or a ticket import.
- **`--html FILE`** — a single self-contained page, no network access, light and
  dark aware. The severity tiles link to per-severity sections.
- **`--summary FILE`** — a Markdown list of the exact edit each source file
  needs, plus the recompile-only build list.
- **`--out DIR`** — all of the above, named for the table.

---

## CLI reference

```
--spec FILE                JSON change specification
--table NAME               table for inline --column/--from/--to
--column NAME              column to widen (repeatable)
--from TYPE                current SQL type, paired positionally with --column
--to TYPE                  new SQL type, paired positionally with --column

--source PATH              source directory or file (repeatable)
--copybook PATH            copybook directory (repeatable)
--pattern GLOB             source filename pattern (default *.pco and *.PCO)
--copybook-ext EXT         extra extension to index as a copybook (repeatable)
--format {fixed,free}      force source format instead of auto-detecting
--max-depth N              stop propagating after N hops
--global-vars              merge same-named inline fields across programs

--json FILE                write JSON findings
--csv FILE                 write CSV findings
--html FILE                write an HTML report
--summary FILE             write a Markdown per-file change list
--out DIR                  write <TABLE>.json, .csv, .html and .md
--include-graph            embed the data-flow graph in the JSON
--quiet                    suppress the terminal report
--no-progress              suppress the stderr progress lines
--verbose                  include the reasoning per finding
--fail-on {CRITICAL,HIGH,MEDIUM,LOW,INFO,NONE}
```

---

## Using it as a library

```python
from pathlib import Path
from cobol_impact_analyzer import ChangeSpec, analyze, build_change, to_text

spec = ChangeSpec(
    changes=[build_change("CUSTOMER", "CUST_NAME", "VARCHAR2(30)", "VARCHAR2(60)")],
    source_paths=[Path("/code/pco")],
    copybook_paths=[Path("/code/copybooks")],
)

result = analyze(spec)
print(to_text(result))

for finding in result.findings:
    if finding.severity.value == "CRITICAL":
        print(finding.node_id, finding.remediation)

print("\n".join(result.ddl))
```

`result.graph` holds the nodes and edges if you want to draw the propagation
yourself.

---

## Limits — read these before you trust a clean run

The tool reports what it can prove from the source it was given. It cannot see:

- **Dynamic SQL.** Statements built into a host variable and run through
  `PREPARE`/`EXECUTE` are opaque. Grep for the column name separately.
- **`SELECT *` and `INSERT` without a column list.** Positional mapping needs
  the table DDL, which this tool does not read. Both are reported as warnings.
- **Programs outside the scan.** A `CALL` to a program not under `--source` is
  reported as a warning, and its parameters are not checked.
- **Cursors declared in an unscanned copybook.** The `FETCH` is recorded but its
  columns are unknown.
- **Non-COBOL consumers.** Extract files, JCL, downstream ETL, reports, and any
  Java/C service reading the same table are out of scope entirely.

Every one of these produces a line in the **WARNINGS** section. A run with
warnings is not a clean run. The `coverage-gap` finding exists specifically to
stop "no impacts found" being mistaken for "safe".

Two other honest caveats:

- Same-named fields declared inline in different programs are kept separate by
  default. If your shop relies on naming conventions rather than shared
  copybooks, pass `--global-vars` — at the cost of some false positives on
  generic names like `WS-TEMP`.
- Widened edited pictures (`ZZ,ZZZ,ZZ9.99`) are grown by padding leading float
  positions. The suggestion is a starting point; comma grouping still deserves a
  human eye. The original picture is always shown next to it.

---

## Development

```bash
python -m unittest discover -s tests -v
```

No dependencies, no test runner to install. CI runs the same command on 3.12 and
3.13, then executes the analyzer against `examples/` to catch regressions in the
end-to-end path.

Layout:

```
cobol_impact_analyzer/
  models.py     capacity model, nodes, edges, findings
  picture.py    PICTURE and USAGE clause parsing
  sqltypes.py   SQL type parsing and host-variable requirements
  cobolsrc.py   fixed/free format reading, sentence splitting
  copybook.py   data description parsing, COPY resolution
  sqlparse.py   EXEC SQL decomposition into column/host-variable bindings
  pco.py        program parsing: declarations, SQL blocks, data flow
  analyzer.py   graph construction and width propagation
  report.py     text, JSON, CSV and HTML rendering
  cli.py        command line
```

---

## License

MIT. See [LICENSE](LICENSE).
