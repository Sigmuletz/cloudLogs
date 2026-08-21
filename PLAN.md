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

## 2. Ingest (`cloudlogs/parse.py` + `cloudlogs/ingest.py`)

### 2.1 Layered, never-drop parsing

Each layer degrades independently. A line that fails every layer still becomes a
row with `parse_ok: false` and the raw text in `message` + `_raw`.

```
raw line
  └─ L1  json.loads (once, then again if the result is a str)
       └─ L2  envelope field extraction + kubernetes flatten
       └─ L3  regex over `log`:  ^(TS)\s+(LEVEL)\s+\[(Logger).(method):(line)\]\s+\((thread)\)\s*(message)$
            └─ L4  pipe fields inside message: `key: value | key: value | ...`
```

Ingest prints a per-layer summary:

```
564 lines → 564 records
  json ok           564
  log-pattern ok    564
  pipe-fields ok     51
  parse_ok=false      0
```

### 2.2 Output record schema

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
  "service":              "ram",            // see §2.4
  "req_path":             "/v3/internal/records/getGeneralConsent",
  "req_status_code":      404,              // int
  "req_duration_ms":      114,              // int
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
  "has_response_payload": true,             // bool
  "source_file":          "example/logs.log",
  "parse_ok":             true,
  "_raw":                 { /* untouched decoded original */ }
}
```

* `_raw` is **drawer-only** — never offered as a table column or a filter.
* Dropped entirely: `_p`, `stream`, `file`, `tag`, and single-valued labels
  (`component`, `part-of`, `product-id`, `helm_sh/chart`).

### 2.3 `log` string parsing

Pattern (all 564 sample lines match):

```
2026-07-09 08:25:06 WARN  [OperationLogInterceptor.filter:44] (executor-thread-44978) | path: ...
└── app_time ──┘ └level┘  └─ logger ─┘ └method┘ └line┘  └──── thread ────┘  └── message ──
```

Pipe fields (L4) seen: `path`, `response status code`, `x-request-id`,
`x-useragent`, `user-agent`, `time(ms)`, `Host header`,
`X-Forwarded-For header`, `HasResponsePayload`.

They **backfill** `req_*` only where the prefixed twins are absent (5 of 51 rows);
`x-request-id` always lands in `op_x_request_id`. `message` keeps its full
original text either way.

### 2.4 Prefix collapse and `service`

`ram_*` and `information_*` are the same semantic columns under a per-component
prefix. Collapse both to `req_*` and record which prefix supplied them.

```
prefix present            → service = "ram" | "information"
else container_name       → "security-gate" | "notification" | "information" | "ram"
no kubernetes key         → "unknown"
```

No `tenant` column — the insurer stays visible via `k8s_namespace` / `k8s_pod`.

### 2.5 Column metadata (`columns.json`, emitted next to `logs.json`)

Ingest classifies every column so the UI knows which widget to render:

| kind | rule | widget |
|---|---|---|
| `facet` | ≤ 200 distinct values, **including numeric columns with ≤ 25 distinct** (e.g. `req_status_code`) | checkbox list (+ value search when > 12) |
| `number` | int/float column with > 25 distinct (e.g. `req_duration_ms`, `src_line`) | min/max inputs |
| `time` | `time`, `app_time` | from/to range |
| `text` | everything else | substring input, regex toggle |

Each entry: `{name, kind, label, distinct, numeric, default_visible, default_filter}`.

### 2.6 CLI

```bash
python -m cloudlogs.ingest example/logs.log                # → data/logs.json
python -m cloudlogs.ingest 'logs/**/*.log' -o data/all.json
python -m cloudlogs.ingest logdir/                          # recurses *.log
```

Accepts files, globs and directories; adds `source_file` per record.
Server auto-runs ingest at startup when `data/logs.json` is missing or older
than any input.

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
* **Reorder**: HTML5 drag-and-drop on headers, drop indicator between columns.
* **Sort**: click cycles asc → desc → off; shift-click appends a key with a
  `¹²³` precedence badge. Server-side; scroll resets to top.
* **Infinite scroll**: fetch next 200 when within 300px of the bottom.
* **Row click** → right drawer: every normalized field + pretty-printed `_raw`.
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
  pyproject.toml          fastapi, uvicorn[standard], pytest
  run.sh                  ingest-if-stale, then uvicorn :8000 (Linux/macOS/WSL)
  run.ps1                 the same, for Windows PowerShell
  .gitignore
  cloudlogs/
    __init__.py
    parse.py              line → record (pure, no I/O)
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
    test_parse.py
    test_query.py
    test_lucene.py
```

Run:

```bash
./run.sh                      # ingest-if-stale + uvicorn --reload :8000
pytest                        # parse + query suites
```

```powershell
.\run.ps1                     # same thing on Windows, outside WSL
```

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

`tests/test_parse.py`

* double-decode (string-wrapped JSON and plain JSON)
* envelope flatten + label pruning
* `log` regex: level, logger, method, src_line, thread, message
* prefix collapse `ram_*` / `information_*` → `req_*`, `service` derivation
* pipe-field extraction and `req_*` backfill precedence
* type coercion: `"404"` → `404`, `"true"` → `True`
* malformed input: not JSON, JSON but no `log`, `log` not matching the pattern
  → row still produced with `parse_ok: false`, nothing lost
* missing `kubernetes` key → `service: "unknown"`, k8s columns null

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
