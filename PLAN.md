# cloudlogs — Log Viewer & Filter

Python-backed web app for viewing and filtering Kubernetes/Quarkus JSON logs.
Two stages: **ingest** (`.log` → normalized `.json`) and **serve** (FastAPI API +
vanilla-JS table UI with server-side filtering, sorting and faceting).

---

## 1. Source data

`example/logs.log` — 564 lines. Each line is a **JSON string containing JSON**
(double-encoded: `json.loads(json.loads(line))`).

Envelope keys observed:

| key | present | notes |
|---|---|---|
| `_p` | 564 | always `"F"` — dropped |
| `stream` | 564 | always `"stdout"` — dropped |
| `file` | 564 | duplicates pod/namespace/container — dropped |
| `tag` | 564 | duplicates pod/namespace/container — dropped |
| `time` | 564 | RFC3339 ns, `+00:00` — **canonical timestamp** |
| `log` | 564 | the raw application log line (see §2.3) |
| `node_name` | 564 | 7 distinct |
| `kubernetes` | 560 | nested: `cluster_name`, `container_name`, `namespace_name`, `pod_name`, `labels{...}` |
| `x_trace_id` | 86 | uuid |
| `has_response_payload` | 46 | `"true"`/`"false"` |
| `ram_*` | 46 | 7 suffixes: `log_level, path, status_code, duration_ms, host, user_agent, x_header` |
| `information_*` | 4 | same 7 suffixes |

`kubernetes.labels` mostly constant; keep only the informative ones.

Levels in the sample: DEBUG 355, INFO 161, WARN 47, ERROR 1. No multiline logs.
Max `log` length 569 chars. 51 lines carry pipe-delimited operation-log fields.

---

## 2. Ingest (`rules.yaml` + `cloudlogs/parse.py` + `cloudlogs/ingest.py`)

Ingest is **configuration, not code**. `rules.yaml` at the project root declares
every column and every extraction rule; `parse.py` is a rule engine with no
mapping tables of its own. Adding a column, adding a second rule to an existing
column, or extracting a new value with a regex is an edit to that one file.

### 2.1 `rules.yaml`

Git-tracked, edited in place. `--rules PATH` or `CLOUDLOGS_RULES=PATH` points at
a different file for a one-off; there is no merging or layering — the file in
use is the whole truth. Its mtime feeds `is_stale()`, so editing rules alone
triggers a re-ingest.

Two blocks:

```yaml
columns:
  - {name: time,            type: time}
  - {name: level,           kind: facet}
  - {name: message,         kind: text}
  - {name: req_status_code, type: int}
  - {name: req_duration_ms, type: int}
  - {name: trace_span}                      # kind auto-classified
  - {name: pod_ref}

rules:
  - name: log-line                          # optional; defaults to target
    required: true                          # counts toward parse_ok
    from: log
    regex: '^(?P<app_time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<level>\S+)\s+\[(?P<origin>[^\]]*)\]\s+\((?P<thread>[^)]*)\)\s*(?P<message>.*)$'

  - target: req_status_code                 # first non-null wins
    from: [ram_status_code, information_status_code]
  - target: req_status_code                 # only fills what is still empty
    from: message
    regex: '(?:^|\|)\s*response status code:\s*([^|]+)'

  - target: pod_ref                          # declared above like any column
    join: [k8s_namespace, k8s_pod]
    sep: '/'
```

`columns:` is the schema — it fixes column **order**, the `type` used for
casting, and any UI override (`kind`, `label`, `default_visible`). `rules:` is
an ordered pipeline.

### 2.2 What one rule can do

| key | meaning |
|---|---|
| `name` | label in the per-rule summary; defaults to `target` |
| `target` | the column to write; must be declared in `columns:` |
| `from` | source, or a list of sources tried in order (first non-null wins) |
| `regex` | applied to the source; **group 1** → `target`, **named groups** → those columns |
| `join` + `sep` | concatenate several sources into `target`, skipping nulls |
| `all` + `sep` | keep **every** match of `regex`, not only the first, joined with `sep` (default: one per line) |
| `required` | this rule's success counts toward `parse_ok` |

A rule with named groups needs no `target`. A rule with `join` needs no
`from`/`regex`. There is no `when:`, no transform chain and no template
language — a second rule, or a better regex, covers those cases.

`all: true` is for a value that legitimately occurs more than once in one line
— trace ids, repeated headers. The column stays a plain string, so filtering,
sorting and the query language need no notion of a list:

```yaml
  - {name: trace-ids, target: req_trace_ids, from: message, all: true,
     regex: '(?i)(\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?)'}
```

It takes exactly one capture group (or none, meaning the whole match), and is
rejected alongside named groups or `join`, where "which value repeats" would be
undefined. Duplicates are kept in the order they appear; a column that collects
matches should be `type: str`, since a multi-line value will not cast to a
number.

### 2.3 Source addressing

Rules read one namespace: a working dict that starts as the decoded envelope
and accumulates the columns earlier rules produced. A produced column shadows a
raw key of the same name.

```yaml
from: kubernetes.labels.pod-template-hash    # dotted path walks nesting
from: message                                # the column, once a rule wrote it
from: 'labels.app_kubernetes_io/version'     # quote a key with a literal dot
```

### 2.4 Order and precedence

Rules run top to bottom. **A rule only writes a column that is still empty** —
so the first rule to produce a non-null value wins, and reordering the file is
how you change priority. There is no `overwrite:` flag.

### 2.5 Folding alternative spellings (`map:`)

One log source writes `WARN`, another writes `warning`, a third `INFO0`. Left
alone they are three facet rows and `level:WARN` finds only one of them. A
column may declare `map:` to fold them onto one value:

```yaml
columns:
  - name: level
    kind: facet
    map:
      warning: WARN
      info0: INFO
      fatal: ERROR
```

Keys are matched **case-insensitively** against whatever a rule wrote, and a
value nobody listed passes through as written. The fold happens at write time,
before casting, so a later rule reading that column sees the canonical value
too. Mapped values are checked against the column's `type:` at load, and two
keys differing only by case are an error rather than a silent winner. The
original spelling is still in `_raw` and in the log line the drawer shows.

### 2.6 Types and casting

`type:` is one of `str` (default), `int`, `float`, `bool`, `time`. A value that
will not cast becomes **null**, and the failure is counted and reported. A typed
column therefore holds that type or nothing — `req_duration_ms:>=100` can never
meet a string.

### 2.7 Never-drop parsing

L1 is fixed: `json.loads` once, again when the result is a string. A line that
is not JSON becomes `{message: <raw text>}` and the rules run over it anyway, so
a plain-text log format is a regex rule rather than new configuration. A line
that fails everything still becomes a row with `parse_ok: false` and its text in
`message` + `_raw`. A record that decoded but carries no log text at all leaves
`message` null and keeps the payload in `_raw`, which the drawer shows.
Nothing is ever dropped.

### 2.8 Validation

The whole file is validated before a single line is parsed: YAML syntax, every
regex compiled, every `target` present in `columns:`, every `type` known, every
`from`/`join` well formed. The first error aborts with the file, the line and a
caret; the CLI exits **2** and the server refuses to start. A half-applied
ruleset is never possible.

```
rules.yaml:14: invalid regex in rule 'status'
    regex: 'status code: (\d+'
                        ^ missing ), unterminated subpattern

rules.yaml:22: rule targets unknown column 'req_stauts_code'
    did you mean 'req_status_code'?
```

### 2.9 Engine-provided columns

`source_file` and `parse_ok` are appended after the declared columns, and `_raw`
is attached to every record. They cannot be declared, reordered or written by a
rule — a rule targeting one is a validation error. `_raw` stays **drawer-only**:
never a table column, never a filter.

### 2.10 Output record schema

The declared columns, in declaration order, then the engine's own. With the
shipped `rules.yaml` that reproduces today's 30 columns exactly:

```jsonc
{
  "time":                 "2026-07-09T08:25:06.166782072+00:00", // canonical, sort key
  "app_time":             "2026-07-09 08:25:06",                 // from the log string
  "level":                "WARN",
  "logger":               "OperationLogInterceptor",
  "method":               "filter",
  "src_line":             44,
  "thread":               "executor-thread-44978",
  "message":              "path: /v3/... | response status code: 404 | ...",
  "service":              "ram",
  "req_path":             "/v3/internal/records/getGeneralConsent",
  "req_status_code":      404,
  "req_duration_ms":      114,
  "req_host":             "pu-epa-aoknds-ram-private....svc.cluster.local:11443",
  "req_user_agent":       "Apache-HttpClient/4.5.14",
  "req_x_header":         "unknown",
  "op_x_request_id":      "005056a2-18c4-1fd1-9ed9-5b6297c1a000",
  "k8s_cluster":          "epa-clstr-prod-be-z1",
  "k8s_namespace":        "pu-epa-system-aoknds-ram",
  "k8s_pod":              "pu-epa-aoknds-ram-5b69569657-7829g",
  "k8s_container":        "pu-epa-aoknds-ram",
  "k8s_pod_hash":         "5b69569657",
  "k8s_version":          "ram-epa-3.1.3-32",
  "k8s_revision":         "v-4.31.159",
  "k8s_deployment_id":    "E1241",
  "k8s_instance":         "muc-pu-epa-aoknds-ram",
  "node_name":            "epa-be-prod-z1-cmpt1-c02",
  "x_trace_id":           "c074e9a7-8336-4323-885c-5dba361c24be",
  "has_response_payload": true,
  "source_file":          "example/logs.log",   // engine
  "parse_ok":             true,                 // engine
  "_raw":                 { /* untouched decoded original */ }
}
```

Dropped by simply not being declared: `_p`, `stream`, `file`, `tag`, and the
single-valued labels (`component`, `part-of`, `product-id`, `helm_sh/chart`).

**Nothing collapses by prefix any more.** `ram_*` and `information_*` are
ordinary sources listed on the `req_*` columns that want them, and `service` is
an ordinary rule. What feeds what is readable off `rules.yaml`, in order, with
no autodetection and no heuristics hidden in Python.

### 2.11 Column metadata (`columns.json`, emitted next to `logs.json`)

A column's `kind` comes from `rules.yaml` when declared; otherwise it is
classified from the data exactly as before:

| kind | rule | widget |
|---|---|---|
| `facet` | ≤ 200 distinct values, **including numeric columns with ≤ 25 distinct** (e.g. `req_status_code`) | checkbox list (+ value search when > 12) |
| `number` | int/float column with > 25 distinct (e.g. `req_duration_ms`, `src_line`) | min/max inputs |
| `time` | `type: time` columns (`time`, `app_time`) | from/to range |
| `text` | everything else | substring input, regex toggle |

Each entry: `{name, kind, label, distinct, numeric, default_visible, default_filter}`.
The file's shape is unchanged, so `query.py`, `lucene.py` and the frontend need
no change: a column added in `rules.yaml` shows up in the UI on its own.

### 2.12 Reporting

Ingest counts how many lines each rule matched and how many values failed to
cast. A rule that never fires is flagged — the most common thing to get wrong
about a new regex is that it silently matches nothing.

```
564 lines → 564 records
  json ok                    564
  log-line       required    564
  message/raw                  0  · matched 564, never needed
  pipe/status-code            51
  pipe/x-request-id           51
  trace_span                   0  ⚠ never matched
  parse_ok=false               0
  ⚠ req_status_code: 3 values could not cast to int, e.g. 'n/a' (line 118)
  → data/logs.json (30 columns → data/columns.json)
```

The count is what a rule **contributed**, not what it matched — a rule whose
column an earlier rule already filled wrote nothing, however often its pattern
hit. The two idle rules are flagged apart: `⚠ never matched` is probably a
mistake and nothing else in the output would reveal it, while `· never needed`
is a fallback this particular input did not require, which is a rule doing its
job.

`parse_ok` is true when the JSON decoded **and** every `required` rule matched.

### 2.13 CLI

```bash
python -m cloudlogs.ingest example/logs.log                # → data/logs.json
python -m cloudlogs.ingest 'logs/**/*.log' -o data/all.json
python -m cloudlogs.ingest logdir/                          # recurses *.log
python -m cloudlogs.ingest logs.log --rules experiments.yaml
```

Accepts files, globs and directories; adds `source_file` per record. The server
auto-runs ingest at startup when `data/logs.json` is missing, older than any
input, older than `rules.yaml` — **or produced from a different set of inputs**.
That last test matters because a log copied off a server keeps its original
timestamp: an mtime comparison alone would call the output "newer" and serve
the previous file's records under the new file's name. Ingest writes
`data/ingest.json` naming the files it read, and the staleness check compares
against it.

---

## 3. API (`cloudlogs/main.py` + `cloudlogs/query.py`)

Records live in memory: `RECORDS: list[dict]`, loaded from `data/logs.json` at
startup. All filtering, sorting and faceting happen server-side in `query.py`.

### `POST /api/logs`

```jsonc
// request
{
  "filters": {
    "level":         {"kind": "facet",  "values": ["WARN", "ERROR"]},
    "k8s_namespace": {"kind": "facet",  "values": ["pu-epa-system-aoknds-ram"]},
    "req_duration_ms": {"kind": "number", "min": 100, "max": null},
    "time":          {"kind": "time",   "from": "2026-07-09T08:00:00Z", "to": null},
    "message":       {"kind": "text",   "value": "404", "regex": false}
  },
  "q": "getGeneralConsent",              // global search across text columns
  "sort": [{"col": "level", "dir": "asc"}, {"col": "time", "dir": "desc"}],
  "limit": 200,
  "offset": 0
}

// response
{
  "rows":   [ /* records, minus _raw */ ],
  "total":  47,
  "offset": 0,
  "limit":  200,
  "facets": {
    "level": [{"value": "WARN", "count": 47}, {"value": "INFO", "count": 161}, ...],
    "k8s_namespace": [...]
  }
}
```

**Semantics**

* Within one column: checked values **OR** together.
* Across columns: **AND**.
* Facet counts are **cross-filtered** — each column's counts are computed with
  every *other* active filter applied but its own filter excluded, so a count
  predicts the result of ticking that box. Zero-count values are returned
  (`count: 0`) and greyed by the UI, never hidden.
* Sorting is multi-key, server-side, over the whole filtered set. Nulls always
  sort last. Numeric columns compare numerically.
* `_raw` is stripped from `/api/logs` rows (fetched on demand by the drawer).
* Every row carries `_idx`, its position in `RECORDS` — that is what the drawer
  passes to `GET /api/row/{idx}`.
* The request may carry an optional `"facet_cols": [str]`; when absent, facets
  are computed for every facet-kind column (25 in the sample).
* The global `q` searches **all** columns (values stringified), not only
  text-kind ones — the box is labelled "search all logs".

### Other routes

| route | purpose |
|---|---|
| `GET /api/columns` | column metadata from `columns.json` |
| `GET /api/row/{idx}` | single record **including** `_raw`, for the detail drawer |
| `POST /api/reload` | re-run ingest and reload `RECORDS` without a restart |
| `GET /` | `static/index.html` |

---

## 3.1 Query language (`cloudlogs/lucene.py`)

`POST /api/logs` accepts a `query` string: a Lucene-style expression, parsed to
an AST and compiled to a `record -> bool` predicate. It is **ANDed** with
`filters` and `q`, and — like `q` — it gates every facet count rather than
being excluded from its own column the way a column filter is.

Records are flat dicts in memory, so the whole language is parse → AST →
predicate. There is no index and no ranking, which is the one part of Lucene
that does not carry over: `^boost` and `~fuzzy` are relevance features and are
**rejected with an explanatory error** rather than silently ignored.

| form | example | meaning |
|---|---|---|
| field term | `level:WARN` | facet fields match the whole value, case-insensitively |
| field term | `message:timeout` | text fields match a substring |
| bare term | `getGeneralConsent` | matches any column |
| phrase | `message:"connection refused"` | quoted; no wildcard interpretation |
| boolean | `a AND b`, `a OR b`, `NOT a` | also `&&`, `\|\|`, `!`, `+a`, `-b`; implicit operator is AND |
| grouping | `service:ram AND (level:WARN OR level:ERROR)` | parentheses |
| field group | `level:(WARN OR ERROR)` | field-scoped |
| range | `req_duration_ms:[100 TO 500]` | `[]` inclusive, `{}` exclusive, mixable |
| open range | `req_duration_ms:>=100` | `>`, `>=`, `<`, `<=` |
| unbounded | `time:[2026-07-09T08:00:00Z TO *]` | `*` on either end |
| period | `time:2026-07-09` | a partial timestamp means that whole period |
| wildcard | `k8s_pod:pu-epa-*-ram-*` | `*` and `?` |
| regex | `logger:/Get.*Service/` | `re.search`, case-insensitive |
| escape | `level:a\:b` | backslash escapes any character |

Field kinds come from `columns.json`, so `req_duration_ms:>=100` compares
numerically and `time:[… TO …]` compares as datetimes. An **unknown field is an
error** — `levle:WARN` → `unknown field 'levle' — did you mean level?` — since
silently matching nothing is the worst possible answer to a typo.

Two tokenizer rules make real log values work: `-` and `+` are operators only
at a clause start (so `pu-epa-aoknds-ram` stays one token), and only the first
`:` of a token splits field from value (so `2026-07-09T08:25:06Z` and
`host.local:11443` stay intact). Values containing `/` or spaces must be
quoted — `req_path:"/v3/records"` — because `/…/` is regex syntax.

A bad query is the user's typo, not a server fault: it returns **400** with
`{"error": …, "pos": …, "kind": "query"}`, and the UI puts a caret at `pos`.

---

## 4. Frontend (`cloudlogs/static/`)

Vanilla JS + CSS. No npm, no bundler, no framework. Dark theme.

### 4.1 Layout

```
┌─ Filters ──────────────┐┌──────────────────────────────────────────────┐
│ [search all logs...  ] ││ 47 of 564 matching            [columns ▾]    │
│ + Add filter        ↻  │├──────────────────────────────────────────────┤
│────────────────────────││ time ↓² ║ level ↑¹ ║ service ║ … ║ message   │
│ ▾ level             ×  ││ 10:25:06 ║ WARN    ║ ram     ║ … ║ path: /v3…│
│   [x] WARN      47     ││ 10:25:06 ║ INFO    ║ ram     ║ … ║ Received… │
│   [ ] INFO     161     ││       ↑ drag header to reorder                │
│   [ ] DEBUG    355     ││       ║ drag divider to resize                │
│ ▾ k8s_namespace     ×  ││                                              │
│   [search values...]   ││  … infinite scroll, 200 rows per page …      │
│   [ ] …-aoknds-ram  12 ││                                              │
│ ▸ req_status_code   ×  │└──────────────────────────────────────────────┘
└────────────────────────┘         row click → right detail drawer
```

### 4.2 Filter panel (slide-in, left)

* Slides in/out; collapsed state remembered.
* Opens with `level`, `logger`, `service`, `k8s_namespace`, `req_status_code`.
* `+ Add filter` → searchable dropdown of **all** columns.
* Each card: header with name, active-count badge, collapse arrow, `×` remove.
  Removing clears that filter and refetches.
* Widget per `kind` (§2.5). Facet lists show `value  count`, greyed at 0,
  with a value-search box when the list is long.
* **Click a facet checkbox to filter; shift-click to highlight** (§4.7).
* Panel membership is **independent** of table column visibility.

### 4.3 Table

* Default columns: `time`, `level`, `service`, `k8s_namespace`, `logger`,
  `req_status_code`, `message` (message takes remaining width).
* **Resize**: mousedown on a `<th>` divider, drag; width persisted per column.
* **Fit to contents**: double-click a divider and the column takes the width of
  its widest loaded value, so a long one is read by scrolling right instead of
  being clipped to an ellipsis (`message` in the sample fits at ~2900px).
  Shift-double-click restores the column's default width. Measurement runs over
  the cells in the DOM — every loaded row is there, the table is not
  virtualised — with each column's own font, and is clamped to 4000px.
* **Reorder**: HTML5 drag-and-drop on headers, drop indicator between columns.
* **Sort**: click cycles asc → desc → off; shift-click appends a key with a
  `¹²³` precedence badge. Server-side; scroll resets to top.
* **Infinite scroll**: fetch next 200 when within 300px of the bottom.
* **Row click** → right drawer: every normalized field + pretty-printed `_raw`.
* **Keyboard**: `↑`/`↓` move the selected row, scrolling it into view and paging
  in more rows at the bottom; `space` opens the drawer on the selection and
  closes it again; `Esc` closes it. With the drawer open the selection drags it
  along, so holding `↓` walks the records one by one. Closing keeps the row
  selected, so the arrows carry on from where you were. Keys are ignored while
  a text field or a context menu has focus.
* **Cell context menu** (right-click or hover chevron): *Filter for value* /
  *Filter out value* / *Copy value* — adds the filter card if absent.
* Level pills: ERROR red (+ row tint), WARN amber, INFO blue, DEBUG grey.
  Status: 5xx red, 4xx amber, 2xx green. Monospace for `time` and `message`.
* `[columns ▾]` picker toggles visibility; hidden columns keep their width.

### 4.4 Time rendering

* `time` is stored UTC and rendered with `Intl.DateTimeFormat` as
  `YYYY-MM-DD HH:MM:SS.mmm`.
* Timezone selector: `Local`, `UTC`, then the full searchable IANA list from
  `Intl.supportedValuesOf('timeZone')`. Persisted with the layout.
* Drawer shows the raw nanosecond value and `app_time` with its delta.

### 4.5 State

| what | where |
|---|---|
| column order, widths, visibility, timezone, panel collapsed, **panel + drawer widths** | `localStorage['cloudlogs.layout']` |
| active filters, sort, global search, **highlights**, **query** | URL hash — shareable, back/forward works |

`#f=level:WARN,ERROR;k8s_namespace:pu-epa-system-aoknds-ram&q=404&sort=-time`

Non-facet kinds extend the same `col:value` grammar:

| kind | form | example |
|---|---|---|
| facet | `col:v1,v2` | `level:WARN,ERROR` |
| number | `col:#min..max` | `req_duration_ms:#100..` |
| time | `col:@from..to` | `time:@2026-07-09T08:00:00Z..` |
| text | `col:/value` (substring), `col:~value` (regex) | `message:/404` |

Highlights ride in their own key, same facet form:
`#f=level:WARN&h=service:ram,information` — so a shared link reproduces what
was highlighted as well as what was filtered.

A **Reset layout** button clears the stored layout.

### 4.6 Query bar

A resizable `<textarea>` above the toolbar, spanning the table's full width —
the only place in the UI that can express OR across columns, NOT, and grouping,
which the per-column panel cannot. The language is §3.1.

* **Ctrl/Cmd+Enter or the Run button executes it** — never on keystroke, since
  a half-typed query is usually a syntax error and flashing red mid-word is
  worse than useless. The Run button carries a `•` while the box differs from
  what is running.
* Resized by the browser's native `resize: vertical` grip; the height is
  remembered in `localStorage` with the rest of the layout.
* A syntax error renders under the box with the offending line and a `^` caret
  at the reported offset, and the caret is placed there in the textarea too.
* `?` opens a help popover: every supported form with a one-line explanation,
  what is deliberately unsupported and why, and every field name as a clickable
  chip that inserts `name:` at the cursor.
* **Clear** empties the query; **Clear filters** in the panel clears the query
  too, since the label promises everything that narrows the table.
* The query lives in the URL hash as `lq=…`, so a link reproduces it.

### 4.7 Highlight mode (shift-click)

Shift-clicking a facet checkbox **highlights** that value instead of filtering
on it. Nothing leaves the table: rows that match keep their normal colours,
rows that do not are dimmed. It answers "where do these sit among everything
else?", which a filter destroys by construction.

Highlights are **OR-ed everywhere** — across values *and* across columns —
because highlighting is additive attention. (Filters are the opposite: OR
within a column, AND across them.) A value can be filtered and highlighted at
the same time; the two selections are independent.

Highlighting is pure client state: the row set never changes, so it never
refetches and never touches the API. It re-paints the loaded rows in place,
keeping the scroll position — losing it would defeat the point.

Because a dimmed table could otherwise be mistaken for a filtered one, filter
and highlight are visually separated everywhere they appear:

| | filter | highlight |
|---|---|---|
| gesture | click | **shift**-click |
| effect on non-matching rows | hidden | dimmed (opacity .30, desaturated) |
| accent | blue (`--accent`) | amber (`--lvl-warn`) with a `◆` |
| facet row | checked box | amber `◆` + amber value text |
| card header | `3` badge | `◆ 3` badge, amber, + amber card edge |
| toolbar | `47 of 564 matching` | `◆ 12 highlighted` |
| strip under the toolbar | — | `◆ Highlighting — nothing is filtered out; non-matching rows are dimmed`, one chip per value, `Clear highlights` |
| tooltip | "…values filtered — non-matching rows are hidden" | "…values highlighted — non-matching rows are dimmed, not hidden" |

Every facet card also carries the literal hint
`click = filter (hides others) · shift-click = ◆ highlight (dims others)`.
The cell context menu offers **Highlight value** / **Remove highlight**
alongside the filter actions, and removing a card clears both its selections.

### 4.8 Resizable side panels

Both side panels are sized by CSS variables (`--panel-w`, `--drawer-w`), so a
drag just rewrites the variable:

* A 5px grip sits on the filter panel's right edge and the drawer's left edge,
  `cursor: col-resize`, tinted on hover and while dragging.
* Drag to resize; **double-click restores the default** (288px / 460px).
* Clamped to 170–720px (panel) and 260–980px (drawer), and further capped so
  the table always keeps at least ~320px. Widths are re-clamped on window
  resize, so shrinking the window can never hide the table.
* Both widths persist in `localStorage` with the rest of the layout, and
  **Reset layout** restores them.

---

## 5. Project layout

```
cloudlogs/
  PLAN.md
  rules.yaml              columns + extraction rules (the ingest config)
  pyproject.toml          fastapi, uvicorn[standard], PyYAML, pytest
  run.sh                  ingest-if-stale, then uvicorn :8000 (Linux/macOS/WSL)
  run.ps1                 the same, for Windows PowerShell
  share-lan.ps1           forward a Windows port to the WSL distro (elevated)
  .gitignore
  cloudlogs/
    __init__.py
    rules.py              rules.yaml → validated Ruleset (load + validate)
    parse.py              rule engine: line + Ruleset → record (pure, no I/O)
    ingest.py             CLI: paths/globs/dirs → data/logs.json + columns.json
    query.py              filter + sort + facet over list[dict]
    lucene.py             Lucene-style query language (parse → AST → predicate)
    main.py               FastAPI app, routes, static mount
    static/
      index.html
      app.js
      style.css
  data/logs.json          generated, gitignored
  data/columns.json       generated, gitignored
  example/logs.log
  tests/
    golden/logs.json      564-record snapshot the engine must reproduce
    golden/columns.json
    test_rules.py
    test_parse.py
    test_startup.py
    test_query.py
    test_lucene.py
```

Run:

```bash
./run.sh                      # ingest-if-stale + uvicorn --reload :8000
./run.sh path/to/app.log      # ingest this file instead of the default
./run.sh a.log b.log logs/    # several files, or a directory
./run.sh 'logs/**/*.log'      # a glob -- quote it so the shell keeps it
./run.sh --help
pytest                        # engine, rules, query and lucene suites
```

The same thing without the shell wrapper — `cloudlogs/main.py` is its own CLI:

```bash
python -m cloudlogs.main                       # default input, :8000
python -m cloudlogs.main path/to/app.log       # ingest this file
python -m cloudlogs.main a.log logs/ 'g/**/*.log'
python -m cloudlogs.main app.log -p 9000 --host 127.0.0.1
python -m cloudlogs.main app.log --rules my.yaml --data /tmp/out.json --reload
```

`python cloudlogs/main.py app.log` works too: run as a file, Python would put
`cloudlogs/` rather than the project root on `sys.path`, so both entry points
put the root back before importing the package. `python -m` needs the package
importable, so run it from the project root — or `pip install -e .` once, which
also installs a bare `cloudlogs` command that works from anywhere.

Paths given on the command line are resolved against the directory you ran the
script in, not the project root, and they override `CLOUDLOGS_INPUT`.

```powershell
.\run.ps1                     # same thing on Windows, outside WSL
.\run.ps1 path\to\app.log     # -Port and -BindHost stay named flags
```

PowerShell may refuse an unsigned script; `powershell -ExecutionPolicy Bypass
-File .\run.ps1` runs it without changing the machine's policy.

Nothing in the code is WSL- or Linux-specific: paths go through `pathlib`,
relative `CLOUDLOGS_INPUT` / `CLOUDLOGS_DATA` entries resolve against the
project root rather than the working directory, and the only dependencies are
fastapi + uvicorn. `CLOUDLOGS_INPUT` accepts `,` or the platform `os.pathsep`
as its separator, so a Windows drive letter (`C:\logs\app.log`) passes through
intact.

### 5.1 Reaching the server from outside its host

`run.sh` / `run.ps1` bind **`0.0.0.0`** by default and print every URL the
viewer answers on. `HOST=127.0.0.1 ./run.sh` (or `.\run.ps1 -BindHost
127.0.0.1`) restores loopback-only.

**There is no authentication.** Binding to all interfaces exposes the logs to
anyone who can reach the port, so use the loopback bind on an untrusted
network, or put it behind something that authenticates.

| from | to | how |
|---|---|---|
| Windows host | server in WSL | `http://localhost:PORT` — WSL2 forwards localhost automatically |
| another LAN host | server in WSL | see the two options below |
| anywhere | server on Windows/macOS/Linux natively | `http://<host-ip>:PORT`, allow the port through the host firewall |

A WSL2 distro sits behind NAT with an IP that changes on every restart, so
LAN clients cannot reach it without one of:

* **Mirrored networking** (Windows 11 22H2+) — in `%UserProfile%\.wslconfig`:

  ```ini
  [wsl2]
  networkingMode=mirrored
  ```

  then `wsl --shutdown`. The distro then shares the Windows network stack and
  the port is reachable at the Windows host's own IP.

* **A port proxy** (any Windows 10/11), in an elevated PowerShell:

  ```powershell
  $wsl = (wsl hostname -I).Split()[0]
  netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 `
      connectport=8000 connectaddress=$wsl
  New-NetFirewallRule -DisplayName "cloudlogs 8000" -Direction Inbound `
      -Action Allow -Protocol TCP -LocalPort 8000
  ```

  Re-run the first two lines after a WSL restart, since the distro IP changes.
  Remove with `netsh interface portproxy delete v4tov4 listenport=8000
  listenaddress=0.0.0.0`.

---

## 6. Testing

`tests/test_parse.py` — the engine, against small inline rulesets

* double-decode (string-wrapped JSON and plain JSON)
* dotted-path sources, produced columns shadowing raw keys
* `regex` group 1 → target; named groups → several columns at once
* `from:` list, first non-null wins
* two rules on one column: the first to produce a value wins, the second only
  fills a blank
* `join` + `sep`, with a null piece dropped rather than rendered as `''`
* casting: `"404"` → `404`, `"true"` → `True`, `"n/a"` → `None` + counted
* malformed input: not JSON, JSON but no `log`, `log` not matching the pattern
  → row still produced with `parse_ok: false`, nothing lost
* a non-JSON line arrives at the rules as `{message: <raw>}`
* engine columns `source_file` / `parse_ok` / `_raw` present and not writable

`tests/test_rules.py` — loading and validation

* every validation error, each with its file, line and message: YAML syntax,
  uncompilable regex, target not in `columns:`, unknown `type`, rule with
  neither `target` nor named groups, `join` without `sep`, rule targeting an
  engine column, duplicate column name
* an unknown target suggests the nearest declared column
* `--rules` / `CLOUDLOGS_RULES` selection, and `is_stale` reacting to the
  rules file's mtime

`tests/test_startup.py` — the server's contract around the rules file

* a broken `rules.yaml` makes `load_state()` raise rather than serve past it,
  **including when `logs.json` is fresh and nothing needs re-ingesting** —
  otherwise the rules are never loaded and the typo is served straight past
* the good path is unaffected: ingest runs and records load
* `POST /api/reload` on a broken file answers **400** with the message the CLI
  would print, and the already-loaded records keep being served — a running
  viewer survives your typo, a starting one refuses

**Golden snapshot** — `tests/golden/logs.json` + `columns.json` were produced by
the pre-rules implementation. The shipped `rules.yaml` must reproduce them
byte-for-byte; that test is what proves the migration lost nothing.

`tests/test_lucene.py`

* every supported form: terms, phrases, wildcards, regex, ranges, open
  comparisons, booleans in all their spellings, grouping, field-scoped groups
* facet fields match exactly, text fields on substring, partial timestamps as
  periods, nulls never satisfying a range
* every error path: syntax, unterminated quote/regex/range, unknown field with
  its suggestion, range without a field, and `^`/`~` rejected as unsupported
* errors carry a position
* real-dataset counts (`level:WARN` = 47, `NOT level:DEBUG` = 209, …)

`tests/test_query.py`

* OR within column, AND across columns
* cross-filtered facet counts exclude the column's own filter
* zero-count values still present in the facet response
* number min/max, time from/to, text substring + regex
* multi-key sort, direction, nulls-last, numeric vs string ordering
* pagination: `total` is the filtered count, `limit`/`offset` slice correctly
* global `q` matches across text columns

UI is manually tested.

---

## 7. Deferred (explicitly out of v1)

* CSV/JSON export of the filtered set
* Auto-refresh / live tail of the source file
* Saved views / named filter presets
* Authentication
