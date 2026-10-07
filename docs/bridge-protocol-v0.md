# modelx-bridge wire protocol v0

> **A note on references.** This document was written alongside lifelib Studio, the browser app
> the bridge was first built for. Its references to PLAN (§3.2, §3.6, §4, §5a), UI-DESIGN (§4.1,
> §4.2), CLAUDE.md, the spikes (S2, S3, S6) and Studio's frontend files (`nodebar.tsx`,
> `trace.tsx`, `formula.tsx`) point at Studio's design documents and code, which are not
> published. They are kept because they record why each decision was made and what was measured
> to make it.

*2026-09-22. Normative for spike S3. Implements PLAN §3.2 for exactly the Phase-1 Explorer plus one
trace call. MUST/SHOULD are RFC-2119. Shapes marked **verified** were read off modelx 0.33.0 /
lifelib 0.17.1 on 2026-09-22; comm behaviour was verified in the JupyterLite kernel (spike S2).*

*Amended 2026-10-05, bridge **0.10.0**: §18 is new — seven additions that modelx-mcp, the MCP server
over this bridge (PLAN §3.6, spike S6), measured it needed: `computed` on `session.info.models[]`,
`argmin` / `argmax` and their labels on every numeric `stats` block, `values: false` on `trace.preds`
and `trace.succs`, `element` on `cells.page`, `label` on `table.get`, `error_display` on a
`formula_error`, and `index_name` on a Series' or DataFrame's handle tag. `protocol` still stays `0`
and **nothing a 0.9.0 client sends gets a different answer**: each is an OPTIONAL result field, or an
OPTIONAL param whose default is the 0.9.0 behaviour. Two further changes were measured with these and
**deferred** — a revision bump on a failed evaluation, and a canonical `display` on `value.get` —
because they change what the shipped UI relies on (§18.8). Each addition has its own `features`
entry, and a client MUST detect the three params before sending one: `trace.*` and `table.get`
silently drop a param they do not know.*

*Amended 2026-09-26, bridge **0.9.0**: §16 (`trace.succs`, and `evaluate` on `trace.preds`) and
§17 (`map.get`) are new, and `formula.get` gains OPTIONAL `kind`, `parameters`, `derived` and
`dynamic` (§6.4). `protocol` still stays `0` and nothing existing changes shape. All of it reads,
and **none of it can evaluate**: these serve the Trace tab and the Model map, which follow the
selection, so each request fires on a click. The `formula.get` fields exist because the formula pane
moved out of the Explorer into its own tab and lost the tree it read "inherited" and "inside an
ItemSpace" from. Detect this revision with the `features` entries `trace.succs` and `map.get`.*

*Amended 2026-09-22, bridge **0.2.0**: §9 (storage and files) and §10 (`table.get` and binary
buffers) are new, and §1's "buffers MUST be empty" is superseded there. **`protocol` stays `0`.**
Everything added is either a new `method` or an OPTIONAL field on an existing result, so a client
built against the original v0 keeps working untouched — which is why the version number must not
move: §1 says a differing `protocol` closes the comm. `session.info.features` (§6.1) is how a client
detects the additions instead of parsing a version string.*

*Amended 2026-09-22, bridge **0.3.0**: §9.4 (`model.open`) is **rewritten**, §9.4.1 (`model.close`)
and §9.7 (the save-location rule, and what boots) are new, and §9.5's fallback save location is
withdrawn. `protocol` still stays `0`; `model.close` is a new method and everything else is an
OPTIONAL field, so a 0.2.0 client keeps working — but its behaviour **changes**, on purpose, and
that is the point of the amendment: 0.2.0 answered `model.open(path)` with whatever model already
held that name, without reading the file, and let that model adopt the path as its save location.
Both were data loss (§9.7). Detect this revision with the `features` entries `model.close` and
`open.conflict` (§6.1), never with the version string.*

*Amended 2026-09-23, bridge **0.7.0**: §13 (`cells.page`) and §10.5 (`table.stats`) are new, and
`value.get` entries gain an OPTIONAL `args` field (§6.5). `protocol` still stays `0` and nothing
existing changes shape. Both new methods **read**, and §13 is the first method in this protocol that
is normatively forbidden from evaluating: it reads one Cells' cached keys and values and returns a
page of them plus a statistic over the WHOLE column, and it returns **no handle at all**, so there is
no LRU, no `not_found` and no stale snapshot on that path. Detect them with the `features` entries
`cells.page` and `table.stats` (§6.1), separately, and detect **before** drawing a grid frame.*

*Amended 2026-09-23, bridge **0.6.0**: §12 (`ref.set`) is new — the second mutating method, and the
more destructive of the two. `protocol` still stays `0` and nothing existing changes shape. It is
advertised separately from `formula.set` because **their blast radii differ by an order of
magnitude**: a formula edit clears what depended on that formula, a reference edit clears every
cached value in the model (§12.1, measured twice). A client that offers this edit must be able to say
which it is about to do.*

*Amended 2026-09-23, bridge **0.5.0**: §11 (`formula.set`) is new — **the first method that changes
a model** rather than reading one or moving a file. `protocol` still stays `0` and nothing existing
changes shape, but `model.changed` gains a fifth `reason`, `edit`, and it is load-bearing: a formula
edit is invisible to both of the kernel's dirty detectors, so that event is the only thing that marks
the model unsaved (§11.1). Detect this revision with the `features` entry `formula.set`, and detect
it **before** offering an editor.*

*Amended 2026-09-22, bridge **0.4.0**: §9.8 (backups) is new and §6.1 gains a `kernel` block.
`protocol` still stays `0` and no method changes shape; both additions are OPTIONAL fields, and both
came out of driving the running app. A plain Save was silently leaving a full second copy of the
model beside it — modelx's `write_model(backup=True)`, up to three rotations — against a browser
quota nobody can see, and nothing in any reply mentioned it; §9.8 makes the default per-runtime and
makes the reply say what it did. The `kernel` block exists because "Restart Python did nothing" could
not be settled from the frontend: there was no way to tell a restarted interpreter from the one
already answering. Detect this revision with the `features` entries `save.backup` and
`kernel.identity`.*

## 1. Transport and handshake

One Jupyter comm, target name **`modelx-bridge`**. The kernel registers it; the frontend opens it.
All JSON payloads travel in the comm message `data`. **Binary parts travel in the comm message's own
`buffers`, never inside `data`** — see §10, which supersedes this paragraph's original rule that
`buffers` be empty. Buffers are structured-clone *copies* in a worker kernel, so they are not
zero-copy; §10.4 measures what they actually buy. A client that ignores `buffers` still works,
because nothing requires them: every method that can use them also answers in pure JSON.
The frontend owns its kernel (`SessionContext`, never a notebook),
bootstraps Python, then calls `kernel.createComm('modelx-bridge')` and
`comm.open({"protocol": 0, "client": "lifelib-studio/0.1"})`. The kernel's target callback MUST
immediately `comm.send` one **hello** message (§2) carrying the same payload as `session.info`: that
removes a round trip at boot and is where versions are agreed. If the kernel's `protocol` differs,
the frontend MUST close the comm and report a mismatch; v0 negotiates nothing beyond equality.

## 2. Envelope and correlation

Every message is a JSON object with a `"type"` discriminator. Exactly four types exist.

| type | direction | fields |
|---|---|---|
| `req` | FE → kernel | `id`, `method`, `params` |
| `res` | kernel → FE | `id`, and **exactly one** of `result` / `error` |
| `evt` | kernel → FE | `event`, `params` (no `id`) |
| `hello` | kernel → FE | `result` (the `session.info` payload), no `id` |

Worked examples of all four types, in both directions, are in §6 and §7.

**Correlation — the one thing implementers get wrong.** `IComm.send()` returns an `IShellFuture`
whose `done` resolves on the *shell reply*, which carries **no application answer**. The answer
arrives later as a separate `comm_msg` on IOPub, delivered to `comm.onMsg`. Therefore:

- The frontend MUST generate `id` (unique per comm; a string, e.g. `"c" + counter++`), store
  `{resolve, reject}` in a pending map keyed by `id`, and settle it from `onMsg`. It MUST NOT await
  the future for the result; it SHOULD catch the future's rejection to fail the request early.
- The kernel MUST echo `id` **verbatim** and MUST send **exactly one** `res` per `req`. Responses MAY
  arrive in any order; a `res` for an unknown `id` MUST be dropped with a warning, never thrown.
- Frontend timeout: 30 s default, but the client SHOULD pause the timer while the kernel status is
  `busy` — comm messages queue behind a running cell (one kernel thread), so a slow answer is normal.
  A timeout is a **client-side** rejection (`code: "timeout"`); the kernel never sends that code.
- No cancellation in v0. On comm close, every pending request is rejected with `code: "disconnected"`.

## 3. Error shape

```jsonc
{"type":"res","id":"c13","error":{"code":"not_found",
 "message":"no object named 'Projection.claimz' in model 'BasicTerm_S'",
 "data":{"obj":"Projection.claimz"}}}
```

`message` is one human-readable line, safe to show in the UI. `data` is optional and free-form.

| code | meaning |
|---|---|
| `bad_request` | malformed envelope, unknown `method`, missing or ill-typed param, result too large |
| `not_found` | no such model / object / node / handle (includes stale node refs, §4) |
| `no_model` | `params.model` omitted and no model is open |
| `formula_error` | modelx raised `FormulaError` while evaluating. `data` carries `{"formula_traceback": "...", "error_obj": "...", "error_args": [...]}`. `error_args` is **codec-encoded** (§5), positional, and addresses the failing node per §4 — not `repr()` strings. *0.10.0:* also `error_display`, that node's display (§18.6) |
| `internal` | anything else; `data.traceback` carries the Python traceback |

Client-only codes, never sent by the kernel: `timeout`, `disconnected`, `protocol_mismatch`.
The kernel MUST NOT let an exception escape the dispatcher: every failure becomes a `res` with
`error`, so the frontend's pending map never leaks.

## 4. Node addressing

A **node** is an object plus its arguments. On the wire, `Projection.pols(t=3)` is
`{"model": "BasicTerm_S", "obj": "Projection.pols", "args": [3]}`.

- `model` — the model's name, as given by `session.info.models[].name`.
- `obj` — modelx's **`namedid`**: the model-relative dotted path, `""` for the model itself. The
  kernel resolves it with `Model._get_from_name(obj)`; for a reference it resolves the parent and
  uses `_get_object(name, as_proxy=True)`, because `_get_from_name` on a ref returns its *value*.
- `args` — **positional**, in `parameters` order, each value codec-encoded (§5); `[]` for an object
  with no arguments. Trailing arguments MAY be omitted; modelx then applies the formula's defaults.
  Keyword form is **not** supported in v0 — `parameters` in the tree payload supplies the labels.

**`obj` is opaque: the frontend MUST NOT construct or parse it**, only echo back strings the kernel
gave it. Verified reason: a dynamic ItemSpace member's `namedid` is `"Projection.__Space1.claims"`
(the internal name, not `Projection[1].claims`), and that string *does* round-trip through
`_get_from_name`. So ItemSpaces need no special syntax — but `__SpaceN` names are **session-scoped**,
reassigned when ItemSpaces are cleared, so a node ref is only guaranteed valid at the `revision` it
was issued at. After `model.changed` a stale ref may return `not_found`; the frontend then
re-resolves from a fresh `tree.get`. For display, never build a string from `obj`: every node and
tree entry carries a `display` field built from modelx's `repr_parent` + `repr`, e.g.
`"BasicTerm_S.Projection[1].claims(t=0)"`.

## 5. Value codec

JSON natives (`null`, `bool`, `int`, finite `float`, `str`, `list`, and a `dict` with `str` keys that
does not itself contain a `"$t"` key) are encoded as themselves. Everything else becomes a **tagged
object** `{"$t": "<tag>", ...}`.

| `$t` | shape | notes |
|---|---|---|
| `tuple` | `{"$t":"tuple","v":[...]}` | the only non-JSON-native type in whole-tree payloads (probe) |
| `num` | `{"$t":"num","v":"NaN"}` — also `"Infinity"`, `"-Infinity"` | JSON has no literal for these |
| `np` | `{"$t":"np","dtype":"float64","v":34.18079328868595}` | numpy scalar; `v` is `.item()`. When that is a non-finite float, `v` is **itself a `num` tag** and the `np` tag is kept: `{"$t":"np","dtype":"float64","v":{"$t":"num","v":"NaN"}}`. **Verified: lifelib cell values are `np.float64` / `np.int64`, not Python scalars** |
| `datetime` | `{"$t":"datetime","v":"2026-09-22T00:00:00"}` | ISO 8601; a date uses `$t":"date"` |
| `str` | `{"$t":"str","v":"<first 4096 chars>","len":120000,"truncated":true}` | oversized strings only |
| `dict` | `{"$t":"dict","items":[[k,v],...]}` | non-string keys, or a dict containing a literal `"$t"` key |
| `mx` | `{"$t":"mx","kind":"Cells","obj":"Projection.claims","display":"claims(t)"}` | a modelx object held as a value; `obj` is addressable per §4 |
| `handle` | below | pandas / numpy containers, and anything oversized |
| `bin` | `{"$t":"bin","buffer":0,"bytes":225283,"media_type":"application/zip","filename":"BasicTerm_S.zip"}` | **0.2.0.** A byte string carried in the message's `buffers` at index `buffer`; §10. Used by `model.export_zip` |
| `opaque` | `{"$t":"opaque","py":"decimal.Decimal","repr":"Decimal('1.5')"}` | fallback; encoding MUST NEVER raise |

**Handles.** A `DataFrame`, `Series`, `Index` or `ndarray` is **always** a handle, whatever its size,
so the frontend has one code path; so is any other value whose encoding would exceed
`limits.max_inline_bytes` (8192).

```jsonc
{"$t":"handle","h":"h7","kind":"Series","dtype":"float64","shape":[151],
 "columns":["zero_spot"],"index_preview":[0,1,2,3,4],
 "preview":[[0.0],[0.00555],[0.00684],[0.00788],[0.00866]],
 "repr":"year\n0    0.00000\n1    0.00555\n..."}
```

`preview` is at most 10 rows x 10 columns of already-encoded scalars. ~~v0 has no method to read a
handle beyond its preview~~ — **superseded in 0.2.0: `table.get` (§10) pages one.** Handles live in a
per-session LRU of `limits.max_handles` (64) entries; an evicted `h` yields `not_found`, and paging a
handle touches it, so a long scroll cannot evict the table being scrolled. **Decoder rule:** an
unknown `$t` MUST render as `value.repr ?? JSON.stringify(value)` and MUST NOT throw — that is what
lets a later version add tags (`bin`, above) without breaking a deployed frontend.

*0.10.0:* a Series' or DataFrame's tag also carries `index_name` — what its labels are (§18.7).

## 6. Methods

| method | params | result |
|---|---|---|
| `session.info` | `{}` | `{protocol, bridge, modelx, python, runtime, models, limits}` |
| `model.open_sample` | `{sample}` | `{model, revision}` |
| `tree.get` | `{model?, obj?, depth?}` | `{model, revision, root}` |
| `formula.get` | `{model?, obj}` | `{source, doc}` |
| `value.get` | `{model?, nodes, evaluate?}` | `{revision, values}` |
| `trace.preds` | `{model?, obj, args}` | `{revision, node, preds}` |

Added in 0.2.0, all optional for a client — §9 and §10:

| method | params | result |
|---|---|---|
| `storage.info` | `{refresh?}` | `{storage}` |
| `files.list` | `{path?}` | `{storage, path, real_path, parent, entries, ...}` |
| `model.open` | `{path, name?, on_conflict?, force?, reload?}` | `{model, revision, path, reused, ...}` |
| `model.save` | `{model?, path?, format?, verify?}` | `{model, path, bytes, verified, persistent, storage}` |
| `model.export_zip` | `{model?, path?, download?, verify?}` | as `model.save`, plus `download` |
| `model.import_zip` | `{path?, name?, filename?, on_conflict?, force?, reload?}` + buffers | as `model.open`, plus `wrote` |
| `table.get` | `{h, row?, rows?, col?, cols?, format?}` | a page (§10.1) |

Added in 0.3.0 — §9.4, §9.4.1, §9.7:

| method | params | result |
|---|---|---|
| `model.close` | `{model?, force?}` | `{model, closed, was_dirty, forced, models}` |

Added in 0.5.0 and 0.6.0 — §11 and §12. **The two methods that change a model:**

| method | params | result |
|---|---|---|
| `formula.set` | `{model?, obj, source, expect?}` | `{source, doc, parameters, changed, cleared, dirty, overrode, renamed_from, ...}` |
| `ref.set` | `{model?, obj, value, expect?, refmode?}` | `{value, value_type, refmode, changed, cleared, dirty, overrode, ...}` |

Added in 0.7.0 — §13 and §10.5. **Two methods that read and MUST NOT change anything:**

| method | params | result |
|---|---|---|
| `cells.page` | `{model?, obj, row?, rows?, around?, format?}` | `{revision, display, params, n_cached, retains, keys, stats, page, ...}` (§13) |
| `table.stats` | `{h, col?}` | `{h, kind, col, name, dtype, total_rows, stats}` (§10.5) |

Extended in 0.10.0 — §18. **No new method**: three take a new OPTIONAL param, and four replies gain
OPTIONAL fields. Every default is the 0.9.0 answer.

| where | new param | the reply gains | § |
|---|---|---|---|
| `session.info` | | `models[].computed` | 18.1 |
| every numeric / integer `stats` block (`table.stats`, `cells.page`) | | `argmin`, `argmax`, `argmin_label`, `argmax_label` | 18.2 |
| `trace.preds`, `trace.succs` | `values?` | (with `false`, no `value` keys) | 18.3 |
| `cells.page` | `element?` | `element`, `element_missing` | 18.4 |
| `table.get` | `label?` | `focus_row`, `label_matches` | 18.5 |
| a `formula_error` (§3) | | `data.error_display` | 18.6 |
| a Series' or DataFrame's handle tag (§5) | | `index_name` | 18.7 |

`model` defaults to the single open model; with none open the kernel returns `no_model`, with several
open and no `model` given it returns `bad_request`.

**Why this cut.** Explorer (`session.info`, `model.open_sample`, `tree.get`) + the Formula editor's
one lazy read + the node bar + the one trace call. Two deliberate adjustments to the suggested set:
`session.info` is *also* pushed as the `hello` handshake, since the frontend needs it before it can
do anything; and **`value.get` takes an array of nodes**, because the node bar, the tree's value
column and the trace list all want many values at once and every round trip queues behind the single
kernel thread.

### 6.1 `session.info` / `hello`

```jsonc
// kernel -> FE, unprompted, on comm open
{"type":"hello","result":{
  "protocol":0,"bridge":"0.4.0","modelx":"0.33.0","python":"3.14.2","runtime":"pyodide",
  "models":[{"name":"BasicTerm_S","revision":1,"path":null,
             "dirty":false,"sample":"BasicTerm_S"}],
  "samples":[{"id":"BasicTerm_S","model":"BasicTerm_S","title":"BasicTerm_S",
              "summary":"lifelib's simple term life projection: ..."}],
  "storage":{"mode":"drive","root":"/drive","writable":true,"persistent":true,
             "reason":null,"drive_root":"/drive","platform":"emscripten",
             "checked_at":"2026-09-22T08:09:12Z",
             "backup_default":false,"backup_reason":"browser storage is one finite quota ...",
             "max_backups":3},
  "boot":{"reason":"sample","sample":"BasicTerm_S","model":"BasicTerm_S","saved":[]},
  "kernel":{"id":"5a3f71d62618df4a","started":"2026-09-22T08:09:04Z","uptime":8.42,
            "boots":1,"runtime":"emscripten"},
  "features":["table.get","buffers","files","storage","model.close","open.conflict",
              "save.backup","kernel.identity","formula.set","ref.set",
              "cells.page","table.stats"],
  "limits":{"max_inline_bytes":8192,"max_message_bytes":1048576,"max_handles":64,
            "max_buffer_bytes":8388608,"max_page_cells":250000,
            "max_series_rows":50000}}}
// FE -> kernel, on demand; the res is the same object with "type":"res","id":"c1"
{"type":"req","id":"c1","method":"session.info","params":{}}
```

`runtime` is one of `"pyodide"`, `"cpython"`. `models` lists every open model with its `revision`
and, from 0.2.0, the storage `path` it was opened from or last saved to (`null` for a sample, which
has no save location until the first Save As). **That `path` is the model's ONE save location**: it
is what a `model.save` with no `path` writes to, and `null` there means a plain Save is refused
(§9.5, §9.7). From 0.3.0 each entry also carries `dirty` — the user's own code has moved this model
since it was last read or saved, so closing or reloading it would discard something (§9.4.1) — and
`sample`, the sample id it was built from, or `null`. From 0.10.0 it carries `computed`, how many
nodes the model has computed, ItemSpaces included (§18.1).

`boot` (0.3.0) is what the kernel's bootstrap decided to open, and why (§9.7):
`reason` is `"sample"` (a first-time visitor got the demo model), `"saved-models"` (this storage
already holds the visitor's own models, so **nothing was opened** and `saved` lists what was found),
`"models-open"` (the bootstrap cell re-ran with models already open), `"failed"` (the sample could
not be built; `error` says why) or `"not-run"`. A UI SHOULD use `saved` to greet a returning visitor
with their own work rather than with an empty Explorer.

`kernel` (0.4.0) identifies **the interpreter that is answering**. `id` is minted once per Python
interpreter and never changes while that interpreter lives; `boots` counts how many times the
bootstrap has been run inside it; `started` and `uptime` are that interpreter's, not the session's.

It exists for one question the frontend could not previously answer: *did a restart happen?* A
kernel restart discards the comm without closing it, the frontend re-runs the bootstrap to recover,
and both paths end in a `hello` that used to look identical to the previous one. So:

| what the frontend sees | what it means |
|---|---|
| a **different** `id` | a new interpreter is answering. **Every** cached model name, handle and revision is stale and MUST be dropped |
| the same `id`, a higher `boots` | the bootstrap was re-run in place. Nothing restarted; the session continues |
| the same `id`, the same `boots` | the same kernel, the same boot. A "Restart" that lands here did not restart anything |

A UI SHOULD hold the last `id` it saw and compare. `MODELX_BRIDGE_READY` is this same payload, so
the comparison is available from the bootstrap's execute reply, before the comm is even connected.

**`storage` is in `hello` on purpose** (0.2.0). It is the §9 block, and putting it in the handshake
means the frontend knows *before it renders anything* whether this kernel has a persistent
filesystem — which is the service-worker race, and the moment the UI has to start telling the truth
about it. `features` lists the additive extensions this kernel implements; a client SHOULD test that
list rather than the `bridge` version string, and MUST tolerate names it does not know.

`samples` is the gallery the kernel offers, in display order: the ids `model.open_sample` accepts,
with the title and one-line summary a UI should show. It exists so the frontend does not keep its
own copy of the list and drift from the kernel's. Aliases are deliberately absent from it. The field
is additive — a client that does not know it ignores it.

### 6.2 `model.open_sample`

`sample` is one of the curated ids the demo ships; anything else is `not_found`, with
`data.available` listing every id the kernel accepts, so a gallery whose cards have drifted can
re-render itself from the reply instead of dead-ending. Idempotent: opening an already-open sample
returns the existing model.

```jsonc
{"type":"req","id":"c2","method":"model.open_sample","params":{"sample":"BasicTerm_S"}}
{"type":"res","id":"c2","result":{"model":"BasicTerm_S","revision":1}}
```

The **canonical id is the model's own name** (`BasicTerm_S`). Older spellings stay registered as
aliases — `termlife` and `basicterm_s` both resolve to `BasicTerm_S` — so a frontend built against
an earlier id opens the real model rather than failing. Aliases resolve but are never offered:
they appear in `data.available` and not in `samples`.

**A sample-opened model is reconstructible, and 0.3.0 depends on that.** The kernel records the
sample id it built each model from (`session.info.models[].sample`), because a model that one
request can rebuild byte for byte is the only model it is ever safe to close on the user's behalf —
which is how `model.open` frees a name the demo sample is squatting on (§9.4) and how `model.close`
decides it needs no `force` (§9.4.1). The moment such a model is saved, or the user's own code moves
it, it stops being reconstructible and is treated like any other model.

### 6.3 `tree.get`

`obj` (default `""`) is the subtree root; `depth` (default `-1`, all) limits recursion. Measured: a
whole lifelib tree is 10–63 KiB and under 1 ms, so v0 fetches it in one shot and `obj`/`depth` exist
only for models that would exceed `max_message_bytes`. `cached` is `len(cells)`, the count the
Explorer shows; `derived` drives the `← base` / `overrides` badges; ItemSpaces are listed by name
under `itemspaces` but **not** recursed into in v0.

```jsonc
{"type":"req","id":"c3","method":"tree.get","params":{"model":"BasicTerm_S","obj":"","depth":-1}}
{"type":"res","id":"c3","result":{"model":"BasicTerm_S","revision":3,"root":{
  "kind":"Model","obj":"","name":"BasicTerm_S","display":"BasicTerm_S","refs":[],"spaces":[
    {"kind":"UserSpace","obj":"Projection","name":"Projection","display":"Projection[point_id]",
     "parameters":["point_id"],"bases":[],"derived":false,"itemspaces":["__Space1"],"spaces":[],
     "cells":[
       {"kind":"Cells","obj":"Projection.claims","name":"claims","display":"claims(t)",
        "parameters":["t"],"has_formula":true,"derived":false,"cached":121}],
     "refs":[
       {"kind":"Reference","obj":"Projection.disc_rate_ann","name":"disc_rate_ann",
        "display":"disc_rate_ann","value_type":"Series","derived":false}]}]}}}
```

### 6.4 `formula.get`

```jsonc
{"type":"req","id":"c4","method":"formula.get","params":{"obj":"Projection.claims"}}
{"type":"res","id":"c4","result":{
  "source":"def claims(t):\n    \"\"\"Claims\n\n    ...\n    \"\"\"\n    return claim_pp(t) * pols_death(t)\n",
  "doc":"Claims\n\nClaims during the period from ``t`` to ``t+1`` ..."}}
```

`source` is modelx's `Formula.source` verbatim, docstring included — the editor folds it, the kernel
does not strip it. `doc` is `Cells.doc`, and MAY be `null`. No formula ⇒ `not_found`.

*0.9.0, all OPTIONAL:* `kind` is the Python class name; `parameters` the formula's parameter names;
`derived` is true for a Cells inherited from a base Space, so an edit will override it (§11.4);
`dynamic` is true for a Cells modelx generated inside an ItemSpace, which `formula.set` refuses
(§11.3). They are on this reply, not on the selection, because they change on exactly the event the
editor causes — editing an inherited Cells makes it defined here — and this reply is re-read after
every edit. Verified 2026-09-26: `doc` here equals `doc.get`'s for all 40 of BasicTerm_S's Cells, so a
client showing a Cells' formula needs no second request for its documentation.

Writing one back is `formula.set` (§11). A client that re-reads this on `model.changed` while an
editor is open MUST NOT treat the re-read as the editor's starting point; see §11.2.

### 6.5 `value.get`

One entry per node, **in request order**. A per-node failure is reported *inside* the entry, not as a
request-level error, so one bad node cannot fail a batch.

```jsonc
{"type":"req","id":"c5","method":"value.get","params":{"model":"BasicTerm_S","evaluate":true,
 "nodes":[{"obj":"Projection.claims","args":[0]},
          {"obj":"Projection.pv_net_cf","args":[]},
          {"obj":"Projection.disc_rate_ann","args":[]}]}}
{"type":"res","id":"c5","result":{"revision":4,"values":[
  {"ok":true,"display":"BasicTerm_S.Projection.claims(t=0)","args":[0],"cached":true,
   "value":{"$t":"np","dtype":"float64","v":34.18079328868595}},
  {"ok":true,"display":"BasicTerm_S.Projection.pv_net_cf()","args":[],"cached":true,
   "value":{"$t":"np","dtype":"float64","v":910.9206262844634}},
  {"ok":true,"display":"BasicTerm_S.Projection.disc_rate_ann","args":[],"cached":true,
   "value":{"$t":"handle","h":"h7","kind":"Series","...":"as in §5"}}]}}
```

`evaluate` (default `true`) may be set to `false` to read only already-cached values; a
not-yet-computed node then returns `{"ok":true,"value":null,"cached":false}` — the UI's `—` /
*Calculate* state. A failure entry is `{"ok":false,"error":{...}}` using §3's shape.

**`args` (0.7.0, OPTIONAL and ignorable)** is the arguments the entry was actually read at,
codec-encoded — the same tuple `display` is built from, so it costs nothing to produce. A Reference
takes none, so its entry carries `[]` whatever the request sent.

It exists because **an entry that cannot say which node it is for can lie by sitting still.** A
frontend that publishes values on a bus keeps the previous entry while a new read is in flight, so
that a panel does not blink; stepping an argument from `t=0` to `t=1` therefore leaves `t=0`'s
number, and its handle id, on the bus labelled `t=1` for at least one frame — plausibly and
silently. A client cannot reconstruct `display` (it does not know the kernel's model prefix) and a
local guess races the bus's own emit order. A client that renders straight off a value SHOULD compare
`args` against the node it is drawing and render a pending state when they differ.

### 6.6 `trace.preds`

The audit call: the node plus its direct precedents with their values — one expansion step of the
Trace tree.

```jsonc
{"type":"req","id":"c6","method":"trace.preds","params":{"obj":"Projection.claims","args":[0]}}
{"type":"res","id":"c6","result":{"revision":4,
 "node":{"obj":"Projection.claims","args":[0],"display":"BasicTerm_S.Projection.claims(t=0)",
         "value":{"$t":"np","dtype":"float64","v":34.18079328868595},
         "predslen":2,"succslen":1},
 "preds":[
  {"obj":"Projection.claim_pp","args":[0],"display":"BasicTerm_S.Projection.claim_pp(t=0)",
   "value":{"$t":"np","dtype":"int64","v":622000},"predslen":1,"succslen":2},
  {"obj":"Projection.pols_death","args":[0],"display":"BasicTerm_S.Projection.pols_death(t=0)",
   "value":{"$t":"np","dtype":"float64","v":5.495304387248545e-05},"predslen":2,"succslen":3}]}}
```

`predslen` / `succslen` let the tree draw expanders without a second call. `trace.preds` evaluates
the node if it is not cached; a formula failure returns `formula_error` (§3) with modelx's node
traceback. The numbers above are the real BasicTerm_S values (verified 2026-09-22).

*0.9.0:* an OPTIONAL `params.evaluate` (default `true`, which is the behaviour above) and a `cached`
field on the result. With `evaluate: false` the call never computes; see §16.

*0.10.0:* an OPTIONAL `params.values` (default `true`, the behaviour above). With `values: false` no
entry carries `value`, and no value is encoded, so no handle is minted; see §18.3.

## 7. Events — one in v0

```jsonc
{"type":"evt","event":"model.changed","params":{"model":"BasicTerm_S","revision":5,"reason":"execute"}}
```

- `revision` is a monotonically increasing integer per model, and a **"may have changed"** signal,
  not proof of a change. `reason` is one of `"execute"` (a `post_execute` in which the model's state
  actually moved), `"evaluate"` (a bridge method evaluated a formula, which changes cached-value
  counts), `"open"` (a model appeared), `"closed"` (a model disappeared), or — since 0.5.0 —
  `"edit"` (a mutating bridge method changed the model; §11). `open` and `closed` are also a cue to
  re-read `session.info`. At most one event per execution — the kernel coalesces and sends it from
  `post_execute`.
- **`execute` and `edit` are the two that mean unsaved work**, and they are separate because the
  answer to "who did this" differs: `execute` is the user's own code, `edit` is a bridge method
  (`formula.set` or `ref.set`), and `edit` is emitted *because* the fingerprint cannot see either
  change on a model with nothing computed (§11.1). A client that does not know `edit` treats it as
  any other change, which is correct; a client that shows an unsaved marker should treat it exactly
  like `execute`.
- **The bump is change-gated, not unconditional.** An earlier draft of this section said the kernel
  bumps "after every `post_execute` that ran user code". That was false in the dangerous direction
  on the runtime this ships to: in the JupyterLite Pyodide kernel **every `comm_msg` fires
  `post_execute`**, so a bump per `post_execute` meant a bump per bridge request — measured at ~935
  events/second on an idle tab. The kernel now compares a cheap per-model fingerprint and emits only
  when it moves, and records that fingerprint when it emits, so an evaluation the bridge performed
  itself is never re-reported by the `post_execute` that follows. Fewer and more accurate events;
  `revision` is still only a "may have changed" signal, and every result still carries one.
- **Loop hazard, stated because it is easy to hit:** the frontend MUST NOT respond to
  `model.changed` by re-issuing an evaluating method (`value.get` at `evaluate: true`, or
  `trace.preds`). Refresh with `tree.get` and `value.get` at `evaluate: false`, neither of which
  bumps the revision. Every result carries a `revision`, so stale answers can be discarded.

## 8. Limits, non-goals, and what v1 adds

- A single comm message SHOULD stay under `max_message_bytes` (1 MiB). A result that cannot fit MUST
  fail with `bad_request` and a message telling the caller to narrow `obj` / `depth`.
- v0 is **read-only**. No `formula.set`, no model mutation of any kind — PLAN §4, Phase 1.
- **Out of band, not protocol:** the `lifelib-studio:reset-environment` command (deletes the
  `/persist` and `JupyterLite Storage` IndexedDB databases, then reloads) is pure frontend. It has to
  work when the kernel is dead, which is the only situation it exists for (S2).
- **Reserved for v1**, named here so v0 does not paint them out: ~~`table.get(h, rows, cols)` with
  binary buffers~~ (0.2.0, §10) · `trace.succs` / `trace.precedents` · `formula.set` and the
  changeset methods · ~~`model.open` / `model.close` over real files~~ (0.2.0 and 0.3.0, §9) ·
  keyword arguments in node addressing · capability flags in `hello` for LSP-style negotiation · a
  cells-level aggregation for the Model map.
- v0 being read-only is what makes `dirty` (§9.4.1) a signal about the **Console**, not about the
  panels: no bridge method edits a model, so anything that moves one came from user code.

## 9. Storage and files (0.2.0)

### 9.1 The storage block, and why it is on every reply

JupyterLite mounts the site's contents at `/drive` **from its service worker**, and it unregisters
then immediately re-registers that worker on every page load. It intermittently loses that race.
When it loses, `/drive` is simply absent and the kernel has no persistent filesystem — silently.
When it wins, the full round trip works: `read_model` → values identical to native → `write_model`
→ re-read → identical value (measured 2026-09-22, 314,270 bytes). **The defect is registration
reliability, not capability.**

So the kernel runs in two worlds and must never lie about which:

```jsonc
{"mode":"drive","root":"/drive","writable":true,"persistent":true,"reason":null,
 "drive_root":"/drive","platform":"emscripten","checked_at":"2026-09-22T08:09:12Z",
 "backup_default":false,"backup_reason":"browser storage is one finite quota and modelx keeps
 up to 3 rotations, so backups are off unless you ask for one","max_backups":3}
```

| `mode` | meaning |
|---|---|
| `drive` | `/drive` is mounted and writable. `persistent: true`. Normal operation |
| `temporary` | no `/drive` (or it is read-only). Files go to `/tmp/lifelib-studio`, which is MEMFS: **`persistent: false`, everything is lost when the tab closes.** `reason` says so in one line fit to show a user |
| `local` | not a browser. There is no root until the host calls `set_storage_root()`; the bridge does not guess one |

`backup_default` (0.4.0) is what a `model.save` with no `backup` will do **here**, with
`backup_reason` in one line fit to show a user and `max_backups` the rotation count modelx keeps.
They are in this block, rather than only in the save reply, so a Save dialog can state its choice
*before* the write instead of explaining it afterwards. §9.8 has the policy.

Every §9 reply carries this block, and `model.save` additionally repeats `persistent` at the top
level. That is deliberate: design/UI-DESIGN.md §4.1's rule is *never report a save that did not
persist*, and the cheapest way to keep a UI honest is to make the truth arrive with the answer
rather than requiring a second call nobody remembers to make.

**The mode can improve without a restart.** The kernel re-probes whenever the last answer was
`temporary`, so a service worker that registers late — or wins the frontend's retry — flips the mode
on the very next file operation. `storage.info {"refresh": true}` forces the check; that is what the
frontend calls after retrying registration. A mode of `drive` is cached: a mounted drive does not
spontaneously unmount, and the probe costs a write and an unlink.

The probe *writes and deletes a dot file* rather than trusting `isdir`. A mount that lists but
cannot be written to would otherwise be reported ready and fail at save time — the same lie, one
step later.

### 9.2 Paths are virtual

**Every path on the wire is rooted at `/` and is relative to the storage root**, never a real kernel
path: `/models/BasicTerm_S`. In `drive` mode that resolves to `/drive/models/BasicTerm_S`; in
`temporary` mode to `/tmp/lifelib-studio/models/BasicTerm_S`. The frontend does no path arithmetic
and the *same request works in either mode*. Every reply also carries `real_path`, for the Console
and for bug reports — it is diagnostic, and MUST NOT be sent back as a `path`, though the kernel
accepts it if it is.

`..` is resolved; one that would escape the root is a `bad_request`, not a clamp. A caller that asked
for something outside the root asked for something the kernel will not do, and quietly serving a
different path is how traversal bugs get written.

### 9.3 `files.list`

```jsonc
{"type":"req","id":"c7","method":"files.list","params":{"path":"/models"}}
{"type":"res","id":"c7","result":{
  "storage":{"mode":"drive","...":"§9.1"},
  "path":"/models","real_path":"/drive/models","parent":"/","count":1,"truncated":false,
  "entries":[
    {"name":"BasicTerm_S","path":"/models/BasicTerm_S","kind":"model","size":324563,
     "size_capped":false,"modified":"2026-09-21T17:02:35Z","is_model":true,
     "model_format":"folder","model_name":"BasicTerm_S",
     "is_backup":false,"backup_of":null,"backup_slot":null}]}}
```

`kind` is `model`, `directory` or `file`; a model is a directory with `_system.json` at its root, or
a `.zip` holding one. `model_name` is the name modelx will give it — read out of the model's
`__init__.py`, which need not match the folder name, and which the Explorer is what shows. Dotfiles
are hidden. Directory sizes are computed only for models (bounded, and the number a user wants);
a walk that exceeds 5000 files reports `size: null, size_capped: true` rather than hanging the one
kernel thread the UI has. At most 1000 entries, with `truncated: true` beyond that.

`is_backup` / `backup_of` / `backup_slot` (0.4.0) mark modelx's own rotations — `BasicTerm_S_BAK1`
beside `BasicTerm_S`, `BT.zip_BAK1` beside `BT.zip` (§9.8). They are still listed, because they are
the user's data and they are spending the user's quota, but they are **labelled and sorted last**:
an unlabelled `_BAK1` folder reads as corruption to someone certain they never created it. A UI
SHOULD dim or group them rather than showing them as models in their own right.

### 9.4 `model.open` — opening a file opens THAT file

```jsonc
{"type":"req","id":"c8","method":"model.open","params":{"path":"/models/BasicTerm_S"}}
{"type":"res","id":"c8","result":{
  "model":"BasicTerm_S","revision":1,"path":"/models/BasicTerm_S",
  "real_path":"/drive/models/BasicTerm_S","model_format":"folder","reused":false,
  "requested":"/models/BasicTerm_S","saved_name":"BasicTerm_S",
  "models":["BasicTerm_S"],"storage":{"...":"§9.1"}}}
```

Opens a folder or a `.zip`. `name` overrides the saved model name.

**The governing rule, and what 0.2.0 got wrong.** modelx keys open models by name, and 0.2.0 turned
that into the answer: a model already open under the target name was returned *without reading the
file*, and the requested path was quietly adopted as that model's save location. Both halves were
wrong, and together they lost work — see §9.7. **A caller that passed a path asked for the file at
that path.** Reuse by name is available, but only when the caller asks for it, or when the model
already open *is* that file.

`on_conflict` says what to do when a model is already open under the target name. It is how a
caller expresses "give me the file" versus "give me what is already open":

| `on_conflict` | when a model of that name is already open |
|---|---|
| `open` *(default)* | **the file is read.** If the open model is that same file (its save location equals `path`) it is returned untouched and `reused: true` — that is not reuse-by-name, it is the same file. Otherwise, if the open model is a reconstructible sample (§6.2) it is closed to free the name and the reply carries `replaced`; if it is anything else the file is opened under a distinct name (`BasicTerm_S_2`) and the reply carries `opened_as`. **Never destructive, and never a no-op.** |
| `reuse` | the open model is returned, `reused: true`, whatever file it came from. Nothing is read. Its **own** save location is what the reply's `path` carries — `null` when it has none — and `requested` carries the path that was asked for. This is "give me what is already open". |
| `replace` | the open model is closed and the file is read under its name. Destructive, so it is refused with `bad_request` unless the model is safe to close by §9.4.1's rule or `force: true` is passed. This is Revert, and the only way to make an open model become the file again. |

`reload: true` is the 0.2.0 spelling of `on_conflict: "replace"` and still works. It does **not**
imply `force`; a `reload` that would discard changes is now refused instead of performed.

Result fields beyond §9.4's example: `reused` is true only when no file was read; `requested` is the
path the caller passed (always present, and the one field that is never rewritten); `saved_name` is
the name found inside the file; `opened_as` appears only when the model was opened under a name
other than `saved_name`; `replaced` (`{"model":…, "sample":…}`) appears when a reconstructible
sample was closed to free the name. Any of the last three comes with a one-line `warning` fit to
show a user. **`opened_as` is not cosmetic**: modelx keeps a model's name inside the model, so
saving that model back to its file writes the new name into it, and the next open reads it under
that name. A UI SHOULD surface the rename rather than hiding it. **`path` is always the returned model's own save location, never an echo of the
request** — that is §9.7's rule expressed in the reply.

*Why the kernel reads the name out of the file first:* `mx.read_model` does **not** refuse a name
clash. It renames the model that was already open to `<name>_BAK1` and warns on stderr — which would
silently leave a stale duplicate behind every re-open. Two different files can still carry the same
saved name; when that happens the result carries `renamed: [...]` and a `warning` line, so the
frontend can say what happened instead of showing a mystery model in its list.

### 9.4.1 `model.close`

```jsonc
{"type":"req","id":"c8b","method":"model.close","params":{"model":"BasicTerm_S"}}
{"type":"res","id":"c8b","result":{
  "model":"BasicTerm_S","closed":true,"was_dirty":false,"forced":false,
  "models":["Imported"]}}
```

Closes one open model. `model` defaults per §6. Closing emits `model.changed` with
`reason: "closed"` (§7), and the frontend SHOULD re-read `session.info`.

**Closing is destructive and there is no undo, so the kernel refuses unless it can point at
something that reconstructs the model** — the file it lives at, or the sample id it was built from.
The refusal is a `bad_request` whose `data` carries `{model, dirty, path, sample}` and whose message
says which of the two conditions failed; `force: true` closes anyway and is reported back as
`forced: true`.

| the model | closes without `force`? |
|---|---|
| has a save location, and has not changed since it was written or read | yes |
| was built from a sample id, has never been saved, has not changed | yes — `model.open_sample` rebuilds it |
| has changed since it was last read or saved (`dirty`) | **no** |
| has no save location and no sample id (built in the Console) | **no** |

**`dirty` has two detectors, because one of them is not enough** and the gap between them is where
work would be lost:

- **the event** — a `model.changed` with `reason: "execute"` was observed for that model (§7): a
  `post_execute` in which its fingerprint moved. This catches structural edits and anything that
  computes;
- **the edit key** — a walk of the model's Spaces and the identity of every Reference value,
  compared against what was recorded when the model was last read, written or built. This catches
  the edit the fingerprint provably cannot see: on a model with nothing computed yet,
  `Projection.point_id = 3` in a Console leaves all three of the fingerprint's counts identical, and
  that assignment is the most likely edit a demo visitor ever makes. The walk excludes ItemSpaces
  and reads no values, so evaluating a model never moves it — **being looked at is not an edit**,
  or every panel click would arm a confirmation dialog.

Either detector makes a model dirty; reading or writing it to storage clears both. Like `revision`
this is still a **"may have changed"** signal and not proof — the key does not compare formula
bodies — so `dirty: false` is not proof of an unchanged model. That is why `force` exists, and why a
UI SHOULD confirm a close it is about to force rather than treating a missing refusal as permission.
A model the kernel has no baseline for counts as dirty: not knowing is not the same as knowing it is
clean, and this answer decides whether something is destroyed.

### 9.5 `model.save` — and save-as

`model.save` with no `path` writes the model back where it came from; **`model.save` with a `path`
IS save-as**, and it moves the model's save location. There is no separate method.

```jsonc
{"type":"req","id":"c9","method":"model.save","params":{"path":"/work/BT","verify":"read"}}
{"type":"res","id":"c9","result":{
  "model":"BasicTerm_S","revision":1,"path":"/work/BT","real_path":"/drive/work/BT",
  "format":"folder","bytes":315073,"verified":"read","verified_spaces":1,
  "persistent":true,"storage":{"...":"§9.1"},
  "backup":{"policy":"auto","enabled":false,"default":false,"max":3,
            "reason":"browser storage is one finite quota and modelx keeps up to 3 rotations, ...",
            "overwrote":true,"kept":null,"paths":[],"bytes":null,
            "note":"the previous version of /work/BT was replaced; no backup was kept"}}}
```

`format` is `folder` or `zip`, defaulted from the path's extension. Parent directories are created.

**A `model.save` with no `path` writes to exactly what `session.info.models[].path` shows, and
nothing else.** A model with `path: null` — one opened by sample id, or read by the user's own code
in the Console — has no save location, and saving it without a `path` is a `bad_request` that says
so. 0.2.0 additionally fell back to modelx's own `model.path` attribute, which meant the location a
plain Save actually wrote to could differ from the one the UI was showing: a sample read out of the
site's shipped content would have silently overwritten that content. Withdrawn in 0.3.0 — there is
one save location, it is the one in `session.info`, and §9.7 says what may create it.

`verify` is the level of proof the reply is allowed to claim, and it is reported back in `verified`:

| `verify` | `verified` | what was actually checked |
|---|---|---|
| `none` | `none` | the write call returned |
| `exists` *(default)* | `exists` | `_system.json` is on disk afterwards, and `bytes` is the real tree size |
| `read` | `read` | the bytes were **read back as a model**, under a throwaway name, and `verified_spaces` counts its spaces |

`persistent` repeats `storage.persistent`. **`verified: "exists"` with `persistent: false` means: the
bytes were written, and they die with this tab.** A UI must render that as a warning, not as "Saved".

`backup` (0.4.0) is §9.8. `params.backup` is `true`, `false`, or `"auto"`/absent for the per-runtime
default; anything else is a `bad_request`.

### 9.6 `model.export_zip` and `model.import_zip`

Export zips the model into storage and, with `download: true`, **also returns the archive itself as a
binary buffer** (§10) under a `bin` tag:

```jsonc
{"type":"req","id":"c10","method":"model.export_zip","params":{"download":true}}
{"type":"res","id":"c10","result":{
  "model":"BasicTerm_S","path":"/exports/BasicTerm_S.zip","bytes":225283,
  "verified":"exists","persistent":true,
  "download":{"$t":"bin","buffer":0,"bytes":225283,"media_type":"application/zip",
              "filename":"BasicTerm_S.zip"},"storage":{"...":"§9.1"}}}
// and the comm message carries buffers: [<225283 bytes>]
```

That buffer is not a convenience. In `temporary` mode the archive lands in MEMFS, where the frontend
cannot reach it through the contents API at all — so the buffer is the **only** way an export leaves
the tab, which is exactly the situation in which the user most needs one. Export is therefore always
offerable, in either mode, which is UI-DESIGN §4.1's second rule.

Export deliberately does **not** change the model's save location, even though `mx.zip_model` sets
`model.path` to the archive. An export that moved the save target would make the next plain Save
silently overwrite the export.

Import is the inverse and takes either route:

- **with a drive** — the frontend writes the user's file through the contents API and passes its
  `path`;
- **without one** — there is no contents API to write through, so the archive comes in on the
  request's `buffers` and the kernel stages it itself, at `/imports/<filename>` by default.

Either way the result is `model.open`'s, plus `wrote: {path, real_path, bytes}` when the kernel did
the writing. A buffer that is not a modelx archive fails with `bad_request` *after* being written, so
the user can still see and delete the file. An archive whose model name is already taken follows
§9.4 like any other open: by default it is opened under a distinct name rather than replacing what
is there, so importing can never discard the model already in the Explorer.

### 9.7 The save-location rule, and what boots (0.3.0)

**A save location may be created by exactly two events: reading that file into that model, or
writing that model to that file.** Nothing else — not opening something else, not exporting, not
listing, not a model's `path` attribute — may cause a model to acquire one.

This is the rule that was missing, and its absence was the sharpest bug in the product. 0.2.0's
`model.open` did `homes.setdefault(name, path)` on its reuse branch, which protected a model that
already had a save location but not one that had none. A demo visitor's kernel always boots with the
pristine sample open and the sample has no save location, so double-clicking their own saved file
aimed the *sample* at it — no file read, no warning, the Files panel showing the model's location
changing to their path — and the next plain Save wrote the pristine sample over their work. The
frontend was blameless; the reply told it the open had succeeded and where the model now lived.

So the rule is structural, not a check. Reuse by name cannot adopt a path because a reuse under
`on_conflict: "open"` happens **only when the model's save location already equals the requested
path** (§9.4), and a reuse under `on_conflict: "reuse"` reports the model's own location instead of
the caller's. There is no third branch in which an open assigns one. A kernel that implements this
section MUST have exactly one writer of that mapping, reachable only from the read path and the save
path, so that adding a branch that adopts a path requires deliberately calling it.

**What boots.** The bootstrap no longer opens the demo sample unconditionally. It:

1. opens nothing when models are already open (the cell was re-run) — `boot.reason: "models-open"`;
2. otherwise scans storage for models this visitor saved, **excluding the shipped sample content
   the site ships into the drive**, and if it finds any opens nothing and reports them in
   `boot.saved` — `reason: "saved-models"`. A returning visitor's first screen is then their own
   work, not a pristine model wearing the same name as the file they saved;
3. otherwise opens the sample — `reason: "sample"`.

A failure to build the sample is `reason: "failed"` with an `error`, and is not fatal: a missing
model is a content problem, not a reason to leave the visitor with no kernel.

### 9.8 Backups — what a save leaves behind (0.4.0)

Observed by driving the app on 2026-09-22: *"Saving over `/models/throwaway` produced
`/models/throwaway_BAK1`, 307 KiB, same timestamp. Nothing in the UI mentions it."*

That is `mx.write_model`'s `backup=True`, which is modelx's default and the right one on a desktop.
It renames whatever is at the target to `<path>_BAK1`, pushing an existing `_BAK1` to `_BAK2` and so
on up to **three** rotations before the oldest is deleted. The suffix is appended to the whole path,
extension included, so `zip_model` over `BT.zip` leaves `BT.zip_BAK1`.

In a browser that default is wrong twice over. **Storage is one finite quota shared by the whole
origin**, so a visitor who saves four times is holding four copies of their model in space they
cannot see or measure — and the copies are not even hidden: the Files panel grows `_BAK1`, `_BAK2`,
`_BAK3` folders the visitor is certain they never created, which reads as corruption. A backup
nobody asked for, nobody can see and nobody can afford is not a feature.

**So the default is per-runtime, and it comes from the storage mode:**

| `storage.mode` | default | why |
|---|---|---|
| `drive` | **off** | one finite quota, up to 4 copies per model, and `_BAK` folders the user never made |
| `temporary` | **off** | nothing here is persistent; a backup would only spend memory that dies with the tab |
| `local` | **on** | modelx's own behaviour. A real filesystem, the backup visible beside the model, and overwriting a model by mistake costs work nothing else can bring back |

`storage.backup_default` carries this, so a UI can say what Save is about to do *before* it does it.

**`params.backup` overrides it** on `model.save` and `model.export_zip`: `true`, `false`, or
`"auto"` (the same as leaving it out). Any other value is a `bad_request` — a typo'd flag must not
quietly resolve to "whatever the default was".

**And whatever happens is reported.** Silence is what made the original defect a defect; the fix is
not only a better default but a reply that can be read out loud:

```jsonc
"backup":{
  "policy":"on",        // "auto" | "on" | "off" -- how it was decided
  "enabled":true,       // what was actually passed to modelx
  "default":false,      // what "auto" would have given here
  "max":3,              // modelx's rotation count
  "reason":"the caller asked for a backup",
  "overwrote":true,                     // something WAS at the path before this write
  "kept":"/work/BT_BAK1",               // where the previous version went, or null
  "paths":["/work/BT_BAK1","/work/BT_BAK2"],   // every backup of this path now in storage
  "bytes":630148,                       // what they cost, or null if it could not be walked
  "note":"the previous version of /work/BT was kept at /work/BT_BAK1; 2 backups of /work/BT
          are in storage, 630148 bytes"}
```

`overwrote: true` with `kept: null` is the honest statement of a plain browser save: **the previous
version is gone and nothing was kept.** A UI SHOULD say that rather than a bare "Saved".

`paths` and `bytes` are reported *even when this save made no backup*, because earlier saves — or a
kernel that predates this section — may have left some, and a UI cannot offer to reclaim quota it is
not told about. Nothing here deletes anything: the bridge has no method that removes a backup, so
reclaiming them is the frontend's contents-API job, and `files.list` marks them (§9.3) so it can find
them.

The boot scan (§9.7) skips `_BAK` directories: they are models by every structural test, but
offering "BasicTerm_S_BAK2" beside "BasicTerm_S" on the Welcome screen is noise at exactly the
moment a returning visitor is trying to recognise their own file.

## 10. Tables and binary buffers (0.2.0)

### 10.1 `table.get`

The way to read a handle (§5) past its 10×10 preview.

```jsonc
{"type":"req","id":"c11","method":"table.get",
 "params":{"h":"h1","row":0,"rows":100,"col":0,"cols":50,"format":"binary"}}
{"type":"res","id":"c11","result":{
  "h":"h1","kind":"DataFrame","shape":[10000,5],
  "row":0,"rows":100,"col":0,"cols":5,"total_rows":10000,"total_cols":5,
  "index":{"name":"point_id","dtype":"int64","js":"BigInt64Array","buffer":0,"bytes":800},
  "columns":[
    {"name":"age_at_entry","dtype":"int64","js":"BigInt64Array","buffer":1,"bytes":800},
    {"name":"sex","dtype":"object","values":["M","M","F","..."]}],
  "format":"binary","buffers":5,"complete":false}}
```

**One result shape, two fillings.** A column is *either* `{buffer, bytes, js}` or `{values}`, so the
frontend has exactly one branch — `"buffer" in col`. A `binary` page may still contain JSON columns:
a column of strings has no typed-array form, and pretending otherwise would mean inventing an
encoding. `dtype` is always the dtype of the array actually encoded.

- `format` is `json` (**default**, and what a v0 client gets by not asking), `binary`, or `auto`,
  which picks `binary` when the page is at least 512 cells. The default stays `json` so that a client
  that does not read `buffers` is never sent any.
- Layout is **column-major**. A DataFrame's columns each have one dtype; its rows do not. Row-major
  binary would need a struct layout and a per-row unpack in JS, where column-major is
  `new Float64Array(buffer)` and nothing else.
- `js` names the TypedArray constructor, so the frontend needs no dtype table. Big-endian arrays are
  byte-swapped kernel-side rather than flagged — a flag the frontend has to honour is a bug waiting
  for hardware nobody has.
- JSON cells are preview-grade: numpy scalars unwrapped to plain JSON numbers (an `np` tag per cell
  would triple a numeric column for no information — the dtype is carried once, on the column), and
  strings over 120 characters clipped to a `str` tag with `truncated: true`.
- Paging **touches** the handle in the LRU, so scrolling a long table cannot evict the table being
  scrolled. An evicted or unknown `h` is `not_found`; re-read the value for a fresh handle.
- 1-D and 2-D only. A 3-D ndarray is a `bad_request` naming its shape rather than a guess at what a
  2-D view of it should mean.
- A page over `limits.max_page_cells` (250,000) is refused **before anything is built**; binary
  buffers are additionally capped by `limits.max_buffer_bytes` (8 MiB). The JSON half is subject to
  `max_message_bytes` as usual — see the note in §10.4 about what that means for JSON pages.
- *0.10.0:* an OPTIONAL `label` in place of `row` starts the page at the row carrying that index
  label, and the reply then carries `focus_row` and `label_matches` (§18.5).

### 10.2 How buffers travel

Binary parts are the comm message's own `buffers`, never inside `data`; bytes are not JSON. A JSON
payload references one by index: `{"$t":"bin","buffer":0,...}` for a whole byte string, or a
column's `"buffer": 1`. Indices are per message, and buffer 0 is the first.

Both directions work. Kernel → frontend is `comm.send(data, buffers=[...])`; frontend → kernel is
`comm.send(data, metadata, buffers)`, and the kernel reads them off the incoming message (checking
both `msg.buffers` and `content.buffers`, because transports differ, and an upload that silently
arrives empty is very hard to see from the frontend). Incoming buffers are normalised to `bytes`
whether they arrive as `memoryview`, `bytes`, or a JS-backed proxy.

A request that fails sends **no** buffers: an `error` reply is always pure JSON.

### 10.3 What buffers do NOT buy

They are structured-clone **copies** across the worker boundary, not shared memory. There is no
zero-copy here and no `SharedArrayBuffer`. The win, measured below, is in encode/decode cost and in
what fits in a message at all — not in avoiding a copy.

### 10.4 Measured: is it worth it? (2026-09-22)

Kernel-side build + `json.dumps` on CPython 3.13, decode in node v24.11.0 as a stand-in for the
browser's JS engine. Times are medians of 7 / 15 runs.

| case | bytes JSON | bytes binary | kernel ms JSON | kernel ms binary | JS decode ms JSON | JS decode ms binary |
|---|---:|---:|---:|---:|---:|---:|
| `model_point_table` 10000×5 (4×int64 + str) | 298,531 | **450,722** | 15.2 | **1.8** | 0.45 | **0.19** |
| same, 100-row page | 3,304 | 5,211 | 0.4 | 0.3 | — | — |
| cashflows 10043×6 float64 | **1,255,950** | **563,247** | **46.9** | **0.4** | 1.78 | **0.15** |
| cashflows 100×6 float64 (page) | 12,898 | 6,424 | 0.9 | 0.3 | — | — |

**The honest answer is "yes, but not for the reason you would guess".**

- **Binary is not always smaller.** On `model_point_table` — int64 columns holding small numbers like
  `47` and `1` — binary is **1.5× LARGER**, because a fixed 8 bytes beats a 1–2 character decimal
  only when the numbers are big. Anyone who ships binary as an obvious size win is wrong here.
- **On float64 it wins on every axis**, and float64 is what a projection actually produces:
  2.2× smaller, 117× cheaper to build, 12× cheaper to decode. A JSON `910.92066093366` is 15
  characters; the double is 8 bytes.
- **The decisive number is the third row.** That float64 page is **1.26 MB as JSON — over
  `max_message_bytes` (1 MiB), so the JSON request simply fails** with `bad_request`. The same page
  is 563 KB as binary, with an 839-byte JSON envelope. Binary is not an optimisation there; it is the
  difference between the method working and not working.
- **Kernel time is the scarce resource.** There is one kernel thread and every comm message queues
  behind it (§2). 46.9 ms of `json.dumps` is 46.9 ms in which nothing else in the UI answers.
- **int64 is exact only in binary.** `JSON.parse("9007199254740993")` is `9007199254740992` in every
  browser. `BigInt64Array` is right. Policy identifiers and sums in minor units reach that range.

So `format: "json"` stays the default and is the right choice for the 100-row pages a grid actually
renders; `binary` is for whole-column reads, for float64, and for anything a chart or an export
wants. The frontend should ask for what it needs rather than the kernel guessing — which is what
`auto` is for when it has no opinion.

### 10.5 `table.stats` — one WHOLE column, reduced (0.7.0)

```jsonc
{"type":"req","id":"c12","method":"table.stats","params":{"h":"h1","col":4}}
{"type":"res","id":"c12","result":{
  "h":"h1","kind":"DataFrame","col":4,"name":"sum_assured","dtype":"int64","total_rows":10000,
  "stats":{"scope":"column","kind":"integer","n":10000,"count":10000,"nulls":0,
           "sum":"5060517000","mean":506051.7,"min":"10000","max":"1000000"}}}
```

`col` is a positional column index, indexed exactly as `table.get`'s `col` and defaulting to `0`; an
index past the end is `bad_request`. The handle resolves through the same store as `table.get`, so an
evicted `h` is `not_found` with the same sentence, and reducing a column **touches** it in the LRU.

**The statistic is ALWAYS over the whole column and NEVER over a page.** That is the whole reason
this method exists, and it is normative: *a client MUST NOT present a page-scoped aggregate where a
column total belongs.* MEASURED on BasicTerm_S `claims` (121 cached float64): the whole column is
`sum 5814.680788 min 0.0 max 64.478472`, and its first 100 rows give `sum 4562.909698 min 31.015247
max 61.562898` — a Sum **22% low** and **both** extremes wrong. On a projection column that rises and
then falls, the page's extremes are routinely not the column's, so a frontend reduction over the
rows it happens to hold is not a cheaper version of this number; it is a different and wrong one.

Kernel-side is also the cheap direction, not the expensive one. MEASURED: 0.67 ms to reduce 200,000
float64 (6.1 ms for int64, which accumulates in exact Python ints — see §13.8), and the whole reply
above is **266 bytes**, against 1,600,000 bytes of buffers to ship that one column to JS and reduce
it there. The measured end-to-end cost of the request above, over `model_point_table`'s 10,000-row
`sum_assured`, is 0.36 ms. The column is pulled **whole**, without slicing rows — `table.get`'s slicer
takes a window of both axes, so reusing it would materialise a long column twice and reduce the wrong
half anyway.

The `stats` block is shared with §13 and is described there, including why an integer sum is a
decimal string and why a column of arrays reports nothing at all.

Spend it on an **explicit** gesture — a column header click — not on every page turn. It is one
message for a number nobody asked for otherwise.

---

## 11. Editing a formula (0.5.0)

*Added 2026-09-23, bridge **0.5.0**. `protocol` still stays `0` and nothing existing changes shape.
Detect it with the `features` entry `formula.set` (§6.1) — and a client MUST detect it before
offering an editor, because on an older kernel the request comes back
`bad_request: unknown method` after someone has already typed a formula.*

**This is the first method that changes a model.** Everything before it reads (§6), or moves whole
model files around (§9). `formula.set` rewrites a Cells formula in place, which makes it the first
call that can lose work — and the rules below exist because of what was measured about modelx 0.33
while building it, not because they seemed prudent.

| method | params | result |
|---|---|---|
| `formula.set` | `{model?, obj, source, expect?}` | `{model, revision, obj, display, source, doc, parameters, changed, cleared, dirty, overrode, derived, name, renamed_from}` |

```jsonc
{"type":"req","id":"c9","method":"formula.set","params":{
  "obj":"Projection.claims",
  "source":"def claims(t):\n    return 2 * claim_pp(t) * pols_death(t)\n",
  "expect":"def claims(t):\n    \"\"\"Claims\n    ...\n    \"\"\"\n    return claim_pp(t) * pols_death(t)\n"}}

{"type":"res","id":"c9","result":{
  "model":"BasicTerm_S","revision":4,"obj":"Projection.claims",
  "display":"BasicTerm_S.Projection.claims",
  "source":"def claims(t):\n    return 2 * claim_pp(t) * pols_death(t)\n",
  "doc":null,"parameters":["t"],
  "changed":true,"cleared":260,"dirty":true,
  "overrode":false,"derived":false,"name":"claims","renamed_from":null}}
{"type":"evt","event":"model.changed","params":{"model":"BasicTerm_S","revision":4,"reason":"edit"}}
```

### 11.1 Four measured facts, and what each one forces

**1. The edit is invisible to both dirty detectors, so the kernel marks the model dirty itself.**
`_fingerprint` (§7) is (computed nodes, reference edges, structure size) and `_touch_key` (§9.4.1) is
(structure, Reference identities). On a model with nothing computed yet, rewriting a formula moves
**neither** — measured `(0, 0)` → `(0, 0)`, key unchanged. So `formula.set` bumps with a new
`reason: "edit"`, and that is what adds the model to the dirty set. Without it, `model.close` would
discard a rewritten formula while calling the model untouched. `reason` is now one of `open`,
`closed`, `execute`, `evaluate`, `edit`; a client that does not know `edit` treats it like any other
change, which is correct.

**2. modelx validates before it applies, so a rejected edit changes nothing.** Measured: after a
`SyntaxError` the previous formula was still callable and still returned its old value. There is no
rollback here because none is needed — the method only has to avoid destroying anything of its own
before handing the source over.

**3. A formula MUST be exactly one `def` or one `lambda`.** modelx answers anything else with
`ValueError: invalid function or lambda definition` — measured for an `import` above the def, for two
defs, for a bare expression and for the empty string. This is a real property and worth stating
rather than working around: **a formula cannot smuggle arbitrary statements into the kernel.** The
kernel rewords the refusal, because modelx's phrase is accurate and tells a person nothing.

**4. Re-applying the identical source is not free.** Setting a formula clears the cached values of
that Cells and everything downstream — measured at **260 of BasicTerm_S's 1,832 computed nodes** for
an unchanged `claims`. So an identical `source` is **not applied**, and comes back `changed: false`,
`cleared: 0`. A client MUST NOT treat that as a failure, and MUST NOT report it as an applied edit.

### 11.2 `expect` — optimistic concurrency

`expect` is the source the editor started from. The kernel compares it with the live formula and, if
they differ, refuses with `bad_request` carrying `{conflict: true, current, expected}` — `current`
being what the formula is *now*, so the editor can show it rather than guess.

This is what makes it safe for a formula pane to follow `model.changed`: the pane re-reads on every
change, so the source it is displaying moves under an open editor whenever anything else touches the
model. Sending the freshly-read source as `expect` would sail through this check and overwrite an
edit nobody has seen. **A client with an editor open SHOULD always send `expect`**, and it MUST be
the text the draft was started from, never the latest read.

### 11.3 What is refused, and how

| case | code | how to recognise it |
|---|---|---|
| the source does not parse | `bad_request` | `data.syntax === true`, with `lineno`, `offset`, `text` — positions are **1-based and relative to the `source` that was sent**, so they address the editor's own buffer directly |
| the source is not one `def`/`lambda` | `bad_request` | `data.shape === true`; `data.modelx_message` carries modelx's own words |
| the live formula is not `expect` | `bad_request` | `data.conflict === true`, `data.current` |
| a Reference, a Space, a Model | `bad_request` | `data.kind` |
| a Cells inside an ItemSpace | `bad_request` | `data.dynamic === true` |
| over 20,000 characters | `bad_request` | `data.chars`, `data.max_chars` |
| no such object | `not_found` | — |

**Cells only, on purpose.** A Space's formula *is* its parameter list, and rewriting one rebuilds
every ItemSpace beneath it; a Reference holds a value, not a formula. Both are selectable in an
Explorer, so both get a sentence naming what they are instead of modelx's own error. A dynamic Cells
is reachable from a `trace.preds` list — a node inside an ItemSpace — and modelx refuses it; the
kernel refuses it first, with the reason.

### 11.4 The three things modelx does quietly, which the result says out loud

- **`cleared`** — how many computed nodes the edit invalidated. A UI that says only "Applied" hides
  the cost of the click; this is a quarter of BasicTerm_S's computation for a one-line change.
- **`overrode`** — the edited Cells was inherited from a base space and is now defined here. The base
  keeps its own formula (measured). Nothing in modelx says so, and the shape of the model changed.
- **`renamed_from`** — modelx **rewrites the name in the `def` to the Cells name**. Measured:
  `def something_else(t): return 3.0` set on `claims` is stored as `def claims(t):\n    return 3.0\n`.
  In an editor that re-reads the stored source this looks like the rename quietly undoing itself, so
  the result names what was typed. It is not an error and MUST NOT be refused — renaming a Cells is a
  different operation, which v0 does not have.

`parameters` is in the result because **a formula may change its own signature** — measured, `t`
becoming `t, kind`. The node is then no longer addressable by the arguments on screen, and a client
that carried them over would send the wrong ones.

### 11.5 What is still not here

No `value.set`, no `ref.set`, no `cells.new`, no rename, no delete, no undo. Changing what a formula
computes is one thing; changing what the model is made of is a larger surface with the same hazards
and none of the same urgency. Revert in an editor puts back the formula that edit started from;
re-reading the model from storage (§9.4) is the only other way back.

---

## 12. Changing a Reference (0.6.0)

*Added 2026-09-23, bridge **0.6.0**. `protocol` still stays `0`; `ref.set` is a new method and nothing
existing changes shape. Detect it with the `features` entry `ref.set` (§6.1) — separately from
`formula.set`, because a kernel could plausibly have one without the other and because **their blast
radii differ by an order of magnitude**.*

`Projection.point_id = 3` is the edit an actuary reaches for first: it is *"show me a different
policy"*. It is a smaller surface than a formula — no parsing, no signature, no docstring — and a
larger act.

| method | params | result |
|---|---|---|
| `ref.set` | `{model?, obj, value, expect?, refmode?}` | `{model, revision, obj, display, name, parent, value, value_type, refmode, changed, cleared, dirty, overrode, derived}` |

```jsonc
{"type":"req","id":"d1","method":"ref.set","params":{
  "obj":"Projection.point_id","value":3,"expect":1}}

{"type":"res","id":"d1","result":{
  "model":"BasicTerm_S","revision":5,"obj":"Projection.point_id",
  "display":"BasicTerm_S.Projection.point_id","name":"point_id","parent":"Projection",
  "value":3,"value_type":"int","refmode":"auto",
  "changed":true,"cleared":1832,"dirty":true,"overrode":false,"derived":false}}
{"type":"evt","event":"model.changed","params":{"model":"BasicTerm_S","revision":5,"reason":"edit"}}
```

`value` and `expect` are **codec-encoded** (§5), in both directions: `3`, `"BEF_FEE"`, `null`,
`[1, 2]`, `{"$t": "tuple", "v": [1, 2]}`. A client sends `expect` explicitly even when it is `null`,
because a Reference holding `None` is a real state and omitting the key means *do not check* rather
than *it was None*.

### 12.1 `cleared` is the whole model, and a client MUST say so before the click

**MEASURED, and this is the single most important sentence in this section.** A formula edit clears
what depended on that formula — 260 of BasicTerm_S's 1,832 computed nodes (§11.1). A reference edit
clears **all 1,832**. Two checks rule out "it only looked global because everything in BasicTerm_S
reads `point_id`":

- A reference **nothing reads** clears the model anyway. Creating and then changing an unused
  reference on a fully computed BasicTerm_S took its tracegraph from 1,832 to 0.
- In a two-branch model where `uses_k` reads `k` and `uses_j` reads `j`, setting `k` cleared
  `uses_j` as well.

modelx invalidates globally on a reference change. So `cleared` is not "what depended on this"; it is
the model's entire computation, and reporting it as the former would understate the click by the
width of the model. **A UI that offers this edit after a long projection SHOULD state the cost while
the editor is open** — `cleared` in the reply is accurate and too late.

For the same reason the no-op guard matters more here than it does for formulas: a `value` the
Reference already holds is **not applied**, and comes back `changed: false`, `cleared: 0`. Setting
`point_id = 1` when it is already `1` would otherwise throw away the whole model for nothing.
Equality is **type-first** — `1` is not `1.0` and not `True`, and assigning either over the other is a
real change.

### 12.2 `expect` — the same optimistic concurrency as §11.2

Compared as **encoded JSON**, not as Python objects: `expect` is the value the frontend was last
shown, and round-tripping the live value through the same encoder is the only comparison that means
*what you were shown is still what is there*. A mismatch is `bad_request` with
`{conflict: true, current, expected}`, `current` being the live **encoded value** — which is why a
client needs a reader that accepts any `Json`, not the string-typed one §11.2 needs.

### 12.3 What is refused, and how

| case | code | how to recognise it |
|---|---|---|
| the current value cannot be edited | `bad_request` | `data.value_type` names the Python type |
| the new value cannot be edited | `bad_request` | `data.value_type` |
| the new value carries an untypable tag | `bad_request` | the codec refuses `handle` / `opaque` / `mx` before this method sees it |
| the live value is not `expect` | `bad_request` | `data.conflict === true`, `data.current` |
| a Cells, a Space, a Model | `bad_request` | `data.kind` |
| a name that is not already a Reference | `not_found` | — |
| `refmode` is not a mode | `bad_request` | `data.refmode` |
| `refmode` on a Model-level Reference | `bad_request` | Model references have no mode |

**Only values that can cross the wire.** A Reference holds any Python object, and BasicTerm_S's own
are a DataFrame, a DataFrame, a Series and the `numpy` and `pandas` modules. Those reach a frontend
as a `handle` or an `opaque` tag (§5) — there is nothing to edit and nothing to send back. Assigning
over one is not merely useless: setting `model_point_table = 3` is accepted by modelx and makes every
formula that reads it raise (measured). So **both the current and the new value** must be a number,
string, boolean, date, `null`, or a list/tuple/dict of those, and a refusal names the type it found.
A client should make the same judgement from what it can see, and offer no editor at all for a
handle, an opaque, an `mx` tag, **or a truncated string** — `{"$t": "str", "truncated": true}` renders
perfectly and writing it back would silently discard the rest.

**An existing Reference only.** Assigning an unknown name creates one — modelx is happy to — and that
is a change to what the model *is*, not to what it holds. Creating, renaming and deleting are §12.5.

### 12.4 `refmode` is validated here because modelx does not

`set_ref(name, value, refmode="sideways")` stores `"sideways"` as the mode, silently — measured. So
`ref.set` checks it against `auto` / `absolute` / `relative`, and **preserves** the existing mode
rather than resetting it: an edit that quietly changed a Reference's mode would be a change nobody
asked for. In practice it never matters for a value this method can set — `refmode` governs how a
Reference to a modelx *object* rebinds under inheritance, and those are refused by §12.3 — which is
exactly why it must not be disturbed. A Model-level Reference has no mode; its `refmode` is `null`
and passing one is refused.

`overrode` works exactly as in §11.4: a Reference inherited from a base space becomes an override
here, and the base keeps its own value (measured).

### 12.5 What is still not here

No `cells.new`, no `ref.new`, no rename, no delete, no undo, and no way to set a Reference to a
DataFrame, an array or a modelx object. Together with §11 the bridge can now change **what a model
computes**; changing **what it is made of** is a Phase 2 surface.

---

## 13. `cells.page` — one Cells' cached values (0.7.0)

*Added 2026-09-23, bridge **0.7.0**. `protocol` stays `0` and nothing existing changes shape. Detect
it with the `features` entry `cells.page` (§6.1), and detect it **before** drawing a grid frame: on
an older kernel the request comes back `bad_request: unknown method` after the UI has already
promised a table.*

After §11 and §12, two methods that change a model, this one goes back to reading — and it is the
first method in this protocol that is normatively forbidden from computing anything.

```jsonc
{"type":"req","id":"c13","method":"cells.page",
 "params":{"obj":"Projection.claims","around":[0],"rows":200,"format":"binary"}}
{"type":"res","id":"c13","result":{
  "revision":1,"obj":"Projection.claims","display":"BasicTerm_S.Projection.claims",
  "params":["t"],"value_name":"claims","value_dtype":"float64","scalar_values":true,
  "n_cached":121,"retains":true,"too_large":null,"focus_row":0,
  "keys":{"integer":true,"min":"0","max":"120","gaps":0},
  "stats":{"scope":"column","kind":"numeric","n":121,"count":121,"nulls":0,
           "sum":5814.680787725242,"mean":48.05521312169622,
           "min":0.0,"max":64.47847187567388},
  "page":{"kind":"DataFrame","shape":[121,2],"row":0,"rows":121,"col":0,"cols":2,
          "total_rows":121,"total_cols":2,
          "index":{"name":"","dtype":"int64","js":"BigInt64Array","buffer":0,"bytes":968},
          "columns":[{"name":"k0","dtype":"int64","js":"BigInt64Array","buffer":1,"bytes":968},
                     {"name":"v","dtype":"float64","js":"Float64Array","buffer":2,"bytes":968}],
          "format":"binary","buffers":3,"complete":true}}}
```

Measured on the shipped BasicTerm_S with the projection computed: a **940-byte** reply of which the
`page` block is 444 bytes, 3 buffers of 968 bytes each, **0.62 ms** end to end, and **0 handles
minted and 0 tracegraph nodes added**.

### 13.1 It MUST NOT evaluate

A kernel implementing `cells.page` **MUST NOT** compute a value the model has not already computed,
and **MUST NOT** change `len(cells)`, the tracegraph or anything else a later request could observe.

PERMITTED modelx calls, each measured at 0 tracegraph nodes against a populated BasicTerm_S
(tracegraph 1832): `len(cells)`, `cells.parameters`, `cells.is_cached`, `k in cells`, `cells.series`,
`series.index.get_indexer([key])`.

FORBIDDEN: `cells[k]`, `cells(...)`, `cells.to_frame(*args)`, `Space.frame`.

Neither forbidden form looks like an evaluator, which is why they are named rather than left to
judgement. MEASURED: `claims[130]` adds **8** tracegraph nodes and grows `len(cells)` 121 → 122 — a
subscript that computes. And `to_frame()` and `to_frame(*args)` are **one name with opposite
semantics** — measured, `to_frame()` adds 0 nodes and `to_frame([150, 151])` adds **16** and grows
`len(cells)` by 2. This method therefore standardises on the `.series` **property**, which takes no
arguments and so cannot be turned into an evaluator by a later edit.

`get_indexer` is a lookup, not a call: it returns `-1` for a key that was never computed, including
`[(9, 9)]` on a MultiIndex, and that `-1` is what a null `focus_row` means. `table.get` and
`table.stats` slice and reduce an object already behind a handle and never reach the model at all.

`Space.frame` is forbidden for reasons beyond evaluation: measured at 46–60 ms against 0.1–0.2 ms, it
silently drops Cells with no cached values, injects a NaN index row that upcasts int64 to float64,
and raises `ValueError` from inside pandas' merge on a Space with same-named parameters of differing
dtypes.

### 13.2 No handle, and what that buys

**The reply carries no `h`, deliberately.** Every page is a fresh read of the live cache, so on this
path there is no LRU, no `not_found` to recover from, and no stale snapshot — a client cannot be
shown a number that predates an edit it has already been told about, because there is nothing held
between requests to show. A client MUST NOT expect to page a `cells.page` result through
`table.get`.

A kernel MAY memoise the materialised series to make paging affordable. If it does, **the model's
`revision` and the cached count MUST both be part of the memo key.** The implementation here keeps
exactly one entry, keyed `(model, obj, revision, n_cached)` and dropped when the model closes.
MEASURED, and the reason it exists: `cells.series` is 0.06 ms at 121 cached values, 2.6 ms at 10,000
and 15 ms at 50,000, where the window slice out of it is 0.05 ms and the page build 0.2 ms — so
without a memo every page turn pays the whole materialisation again.

**The revision alone is not sufficient, and that was measured rather than argued.** A revision is a
*conservative* change detector (§7), not proof of equality, so a memo keyed on it alone can outlive
the cache it describes. Driven into that state, a 121-value memo served a Cells with 122 cached
values and produced `stats` over 121 under `n_cached: 122`, a `page` claiming `total_rows: 122` while
carrying 121 rows, and `keys.gaps: -1`. The cached count is free — a kernel has already read it to
answer `n_cached` and to check `max_series_rows` — and it changes whenever the cache does. For the
same reason, **every field derived from the series MUST be derived from the series that was actually
paged**: `stats`, `keys`, and the page's `row` / `total_rows` / `shape` / `complete`.

### 13.3 The page frame is built by hand: `k0..kn-1` and `v`

The page is an ordinary §10.1 page over a frame this method builds itself: one column per parameter,
named `k0`, `k1`, … in parameter order, then the value column `v`. `params` and `value_name` carry
the real labels; the fixed names cannot collide with a parameter name or with the Cells' own.

It is **not** `reset_index()`. That gives a 1-parameter Cells and an n-parameter Cells two different
layouts — the extra RangeIndex column — and leaves an object-dtype value column untouched. MEASURED
on a 2-parameter Cells, the hand-built frame pages as three clean typed columns (`k0` int64 /
`k1` int64 / `v` int64, all `BigInt64Array`, 0 handles) where the same Cells' `.frame` renders its
index as `{"$t":"tuple","v":[{"$t":"opaque","py":"builtins.int","repr":"1"},…]}`.

The page's own `index` column is a RangeIndex artefact of reusing the §10.1 builder, and a client
SHOULD ignore it (~1.6 KB on a 200-row binary page, accepted).

The page block's `row`, `total_rows`, `shape` and `complete` describe the **whole series**, not the
window that was built — so a pager can read them directly. `rows` is the window's length.

### 13.4 `scalar_values: false` — the guard that protects the handle store

If the series dtype is `object`, the value column is replaced with each element's short repr (clipped
at 120 characters) and `scalar_values` is `false`. There are then no statistics: `stats.kind` is
`"other"`.

This is not defensive padding. MEASURED: a 10-row object-dtype series of ndarrays paged raw minted
**ten handles — one per cell**, because a JSON cell is encoded at exactly the codec's maximum depth
and the depth guard is `>`, so an ndarray cell falls through to the handle branch. A 200-row page
would mint 200 handles against a 64-entry LRU and evict every handle another panel was paging. With
the repr fallback: 0 handles, measured.

The guard MUST branch on the **series** dtype, before the page is built, never on a sample of the
first value. Under pandas 3.0 a column of strings is dtype `str`, not `object`, and pages as a JSON
column minting nothing — so `object` here means "these are not scalars".

### 13.5 The short replies, and the two empty states that are not the same

**A zero-parameter Cells** returns `page: null`, `stats: null` and a `note`, and builds nothing.
MEASURED on `pv_net_cf`: `parameters ()`, `len 1`, and a `.series` indexed `[nan]` — a degenerate
one-row Series that is never worth a round trip and would page as a NaN key. Its value is a
`value.get` away (§6.5).

**More cached values than `limits.max_series_rows`** returns `too_large: {cached, limit}` with no
page and no statistics. The check is against `len(cells)`, which is 0.005 ms, and it happens
**before** `.series` is touched — so a client can say "1,400,000 cached values, too many to page
here" from the reply instead of discovering the limit by freezing the one kernel thread.

`max_series_rows` is **50,000** in this build. MEASURED on CPython 3.13 / pandas 3.0.6 that is ~15 ms
of materialisation; 200,000 is ~60 ms. It is not 200,000 because Pyodide/WASM is typically 3–10×
slower with pandas 3.0.2, which would put a single click at 200–600 ms of frozen kernel thread and
present as a hung kernel. **This number has not yet been measured in the browser.** A probe timing
`cells.series`, the reduction and the page build at 10k / 50k / 200k under Pyodide should set it, and
until it runs, 50,000 is an extrapolation from the CPython curve and nothing in any test suite can
catch it being wrong.

**`retains` means "this Cells KEEPS its values", NOT "this Cells HAS values."** Measured `true` on a
`claims` with zero cached. The two empty states are different replies and need different sentences,
because both otherwise arrive as an empty grid:

| reply | what it means | what a UI should say |
|---|---|---|
| `n_cached: 0`, `retains: true` | nothing has been computed yet | "Nothing computed yet" — and point at whatever computes |
| `retains: false` | this Cells never keeps a value | "This cells does not keep its values" — and offer **no** refresh, because one would never do anything |

### 13.6 `focus_row`, `around`, and fixed blocks

`around` is the codec-encoded arguments of the node the client is following, positional in parameter
order. `focus_row` is that key's position **in the series**, or `null` when the key has not been
computed — `"t = 130 has not been computed"`, with the grid still shown.

When `row` is omitted, the window opens on the **fixed block** containing the focus:
`row = (pos // rows) * rows`. Not a window centred on it. Centring opens a 121-value column at row 51
with *Previous* already live, which reads as if the page lost its beginning; and fixed blocks let a
client tell **locally** whether a new argument is inside the page it already holds, so stepping an
argument across a projection can cost no requests at all. An explicit `row` always wins.

An explicit `row` **past the end of the series lands on the last block instead**, and `page.row` says
where it went. This is not tidiness: a client that remembers `row: 200` and re-reads after an edit
CLEARED the cache — the Re-read path this method exists to make honest — would otherwise be handed an
empty window reported as `complete: true` at a row that does not exist, and print *"rows 201–200 of
50"*. A client SHOULD take `page.row` as authoritative and re-sync its pager from it.

### 13.7 `keys` — did this run to term?

`keys` is the one genuinely new audit fact here. MEASURED on BasicTerm_S: `claims` is 121 values over
`t = 0..120` while `pols_maturity` is 120 over `t = 1..120` — a difference nothing else in this
protocol surfaces. `gaps` is `(max - min + 1)` minus the number of keys in the index it was measured
from, so `0` means a contiguous run. It is counted off that index and not off a separately-read
count, which is what makes it impossible for `gaps` to be negative: `gaps: -1` came back from a
121-key index counted against an `n_cached` of 122, and a number that cannot mean anything is worse
than no block at all.

`min` and `max` are **decimal strings**, for the reason binary int64 columns exist:
`JSON.parse("9007199254740993")` is `9007199254740992` in every browser.

It is `null` unless the Cells has exactly **one integer parameter**. A float or string key has no gap
to count, and a multi-parameter Cells has a cross-product answer nobody has specified — `null` says
so, where a half-filled block would invite a client to print a number for it.

### 13.8 The `stats` block

Shared with §10.5. `scope` rides on the block itself so a client cannot print the number without
being handed what it is over; a UI SHOULD render the scope and the count beside the figures
(*"— over all 121 cached values"*).

`kind` selects which fields are meaningful, and is decided from the **whole column**, never from a
sample of the first value:

| `kind` | when | `sum` / `min` / `max` | `mean` | also |
|---|---|---|---|---|
| `numeric` | float dtypes | JSON numbers, non-finite values tagged `{"$t":"num",…}` (§5) | number | `nulls` counts NaN |
| `integer` | integer, unsigned and bool dtypes | **decimal strings** | number | |
| `text` | string columns | `null` | `null` | `unique` |
| `other` | anything else — object columns of arrays, datetimes, … | `null` | `null` | `note` |

Three things a client should not have to discover:

- **Integer sum/min/max are decimal strings and the sum is accumulated exactly.** MEASURED:
  `np.arange(1, 200001) * 10**13` sums to `1400752841041379328` through numpy's int64 accumulator —
  silently wrapped — and to `200001000000000000000000` exactly, at a cost of 6.4 ms over 200,000
  rows. A client MUST NOT pass these through `Number()`.
- **Every float goes through the §5 non-finite tagging.** An all-NaN column's mean is
  `{"$t":"num","v":"NaN"}`, not a bare `NaN` — a JSON `NaN` is not parseable and would fail the whole
  request with a confusing size error instead of displaying "NaN".
- **A column with no plain numeric form reports `count`, `nulls` and a `note`, never an error.**
  MEASURED: `pd.read_csv` hands back pandas' masked `Int64` for an integer column with one blank
  cell; its dtype kind is `"i"`, so it reaches the integer branch, and `np.asarray` on it raises
  `ValueError: cannot convert float NaN to integer`. A masked column's nulls are dropped before the
  reduction — which is also what keeps the integer sum exact, since `pd.NA` has no int form and the
  alternative is the float accumulator the decimal strings exist to avoid — and anything still
  unreducible comes back as a block with a `note`. A column-header click may not answer `internal`.
- **`other` reports nothing rather than something wrong.** MEASURED: pandas `.sum()` on an object
  column of ragged ndarrays raises `TypeError: operands could not be broadcast together`, and on
  equal-length ones returns an **array**. `describe()` is not the answer either: it upcasts int64 to
  float64, so an integer column would report `min 47.000000`.

*0.10.0:* a `numeric` or `integer` block also says where its extremes are — `argmin`, `argmax` and
their labels (§18.2) — and `cells.page` takes an OPTIONAL `element` that pages one element of each
cached Series value (§18.4).

### 13.9 Strictness, and what is refused

`cells.page` **rejects an unknown param** with `bad_request`, unlike `table.get`, which silently
drops one. A frontend typo must read as a client error, not as a kernel that quietly answered a
different question. A non-Cells `obj` — a Reference, a Space, the Model — is `bad_request` with a
sentence naming what it is; an unknown `obj` is `not_found`.

### 13.10 What is still not here

No sorting, no filtering, no column paging (the frame is at most n+1 columns wide), no `stride` for
charting a long column, and no way to ask for the values a Cells *could* compute. That last one is
deliberate and is the shape of the whole method: this reads what a model **has** computed, never what
it **could**.

## 14. `doc.get` — one object's docstring (0.8.0)

```json
{"id":"41","method":"doc.get","params":{"model":"BasicTerm_S","obj":"Projection"}}
{"id":"41","result":{"obj":"Projection","kind":"UserSpace","doc":"The main Space in the :mod:`~basiclife.BasicTerm_S` model.\n\n..."}}
```

Answers for **any** object — Model, Space, Cells, Reference — and returns `doc: null` when there is
no docstring or it is only whitespace. `kind` is the Python class name, so a caller can tell what it
asked about without a second round trip. A non-string `__doc__` (which exists in the wild) is
normalised to `null` rather than sent as-is.

**It never evaluates.** `doc` is an attribute read on an already-resolved object, which is §7's
standard for every introspection call.

**Why it is not a field on `tree.get`.** Measured on the shipped `BasicTerm_S`: the `Projection`
space's docstring is **6,824 characters** and all **40** of its Cells carry one, **6,552** more on the
browser's Python 3.14 (7,372 on CPython 3.12 and earlier, which keep docstring indentation) —
about **13 KB** added to a payload that is re-read on every `model.changed`, which fires on every
recalculation. It fits within `max_message_bytes` for this model and would not for a model with
thousands of Cells, and paying it per recalculation to show one node's prose is the wrong trade at
any size. So the doc travels for the node that is actually selected, one at a time.

**Why not relax `formula.get` instead.** It answers `not_found` for anything without a formula, and
the formula pane depends on that: returning a null source for a Space would put an empty formula box
under a Space. A Space's docstring is the richest documentation the demo model ships, so it needed a
route that does not distort the one method that already works.

### 14.1 `predslen` / `succslen` on a `value.get` entry (0.8.0)

A `value.get` entry that **has a value** now also carries `predslen` and `succslen` — how many nodes
it reads, and how many read it:

```json
{"ok":true,"display":"BasicTerm_S.Projection.claims(t=3)","args":[3],"cached":true,
 "value":{"$t":"np","dtype":"float64","v":5.1e-05},"predslen":2,"succslen":1}
```

They were already computed by `trace.preds` (§6) and nowhere else, so the Inspector's promised
"this node's precedent/dependent counts" could only appear **after** the visitor asked for a trace,
and `succslen` reached the frontend with nothing reading it. Both are graph lookups on a node the
kernel has already resolved, so they cost nothing and ride with a read the node bar makes anyway.

**They are absent, not zero, on an uncached entry.** A node with no value has no place in the
dependency graph yet, and `succslen: 0` there would read as "nothing depends on this" — a claim, and
a false one. A client must treat absence as "not known", never as none.

## 15. ItemSpaces in `tree.get` (0.8.0)

A Space's `itemspaces` was a list of modelx's internal names:

```json
{"itemspaces": ["__Space1", "__Space2"]}
```

It is now a bounded list of objects, with the true total:

```json
{"itemspaces": {
   "total": 2,
   "shown": [
     {"kind":"ItemSpace","obj":"Projection.__Space1","name":"__Space1",
      "display":"Projection[1]","args":[1]},
     {"kind":"ItemSpace","obj":"Projection.__Space2","name":"__Space2",
      "display":"Projection[3]","args":[3]}]}}
```

`display` is modelx's own `_get_repr()`, which is the name the visitor computed and the only one
they can read; `name` keeps the internal one because it is what `obj` is built from. `obj`
round-trips: `_resolve` hands back the same object, so `doc.get` answers for it and a `value.get` on
a Cells inside it displays `BasicTerm_S.Projection[1].claims(t=3)`.

**Why a bound, and why `total`.** Measured on a synthetic model of 10,000 ItemSpaces: bare names are
**148,894 bytes** on the wire and the named form is **636,674**, a 4.3x rise on a payload `tree.get`
re-reads on every `model.changed` — which fires on every recalculation. Naming them without a bound
would have made the scale case worse while fixing the readability one. `MAX_TREE_ITEMSPACES` is 50;
naming all 10,000 costs 6 ms and reading their `argvalues` 2 ms, so the cost is the wire, not the
work. `total` is the honest count whatever the bound, so a client can say "50 of 10,000" instead of
implying it has them all.

**Still not recursed into.** §6.3 stands: the tree lists a Space's ItemSpaces and does not descend.
Reaching inside one — so a precedent such as `Projection.__Space1.claims` can be located in the tree
— needs on-demand recursion and is not designed yet. Paging and searching past the bound is Phase 2.

## 16. `trace.succs`, and `trace.preds` that does not evaluate (0.9.0)

```json
{"id":"51","method":"trace.succs","params":{"model":"BasicTerm_S","obj":"Projection.claims","args":[3]}}
{"id":"51","result":{"revision":2,"cached":true,
 "node":{"obj":"Projection.claims","args":[3],"display":"BasicTerm_S.Projection.claims(t=3)",
         "value":{"$t":"np","dtype":"float64","v":33.28673249397713},"predslen":2,"succslen":1},
 "succs":[{"obj":"Projection.pv_claims","args":[],"display":"BasicTerm_S.Projection.pv_claims()",
           "value":{"$t":"np","dtype":"float64","v":5501.194898364312},"predslen":123,"succslen":2}]}}
```

The values are real: BasicTerm_S after `pv_net_cf()`, verified 2026-09-26.

The mirror of §6.6 for the other direction: the node and the nodes modelx has recorded as READING
it. `trace.preds` with `evaluate: false` has the same shape with `preds`.

**Neither ever evaluates.** They exist for the Trace tab, which follows the selection, so one fires
on every click; a click must never start a calculation (UI-DESIGN §4.2). There is no evaluating
`trace.succs` at all, because computing a node does not give it dependents: a dependent exists only
once something that reads the node has been computed. **A dependent is therefore a fact about what
has been calculated, not about the formulas** — measured, after `pv_net_cf()` the only dependent of
`claims(3)` is `pv_claims()`, although `net_cf`'s formula reads `claims` too, because nothing
computed `net_cf(3)`. The formula-level question is §17's.

**An uncomputed node answers `cached: false` and carries NO `preds` / `succs` key.** Not an empty
list: `[]` from a node with a value means "this is an input" (preds) or "nothing has read this yet"
(succs), and sending it for a node with no value would make a statement about the model that is
false. `node` is still sent, with `value: null`, so the client can name what it asked about.

*0.10.0:* both take an OPTIONAL `values`. With `false` the node and every neighbour carry no `value`
key — this uncomputed node's `null` included — and no value is encoded (§18.3).

## 17. `map.get` — one Space as its formulas name it (0.9.0)

```jsonc
{"id":"52","method":"map.get","params":{"model":"BasicTerm_S","obj":"Projection"}}
{"id":"52","result":{"model":"BasicTerm_S","revision":1,"obj":"Projection",
 "display":"BasicTerm_S.Projection",
 "cells":[{"name":"claims","obj":"Projection.claims","parameters":["t"],"derived":false}, ...],
 "refs":[{"name":"point_id","obj":"Projection.point_id"}, ...],
 "edges":[["claim_pp","claims"],["pols_death","claims"], ...],
 "ref_edges":[["point_id","model_point"], ...],
 "recursive":["pols_if"],
 "unread":[]}}
```

`edges` are `[from, to]`: `to`'s formula names `from`, so values flow from → to. `recursive` lists
the Cells whose formula names ITSELF — a recursion through time such as `pols_if(t - 1)` — kept out
of `edges` because a self-loop is not a link between two Cells. `unread` lists any Cells whose source
did not parse, so a client can say so rather than draw it with no links, which would read as an
input. `obj` addresses a Space (a UserSpace or an ItemSpace), or the Model: a Model with exactly one
Space maps that Space, and the reply's `obj` names it — so a map can be drawn before anything is
selected — while a Model with several is `bad_request` listing them. A Cells or a Reference is
`bad_request` naming what it is.

**It is read from the formulas' source and it never evaluates.** The mockup's map was built from
modelx's tracegraph after computing the model, and the product cannot do that: nothing in the sample
is computed on a fresh load, and a picture of the model must not cost a projection. Measured on the
shipped BasicTerm_S (2026-09-26, `test_trace.py`): **40 Cells, 82 links, one cycle group** —
`pols_death`, `pols_if`, `pols_lapse`, `pols_maturity` — in ~2.4 ms. The tracegraph after
`result_pv()` + `result_cf()` holds **77** of those links and none that are not in them; the other five
are `check_pv_net_cf`'s four reads and `model_point → sex`, which that calculation never reaches. So
the source is a superset of that calculation and complete before anything is computed.

**What it cannot see.** A name built at run time (`getattr(space, name)`), and a read through another
Space. A name the formula binds itself — a parameter, an assignment target, a comprehension variable,
a lambda's argument — is not counted as a read of the Cells that shares it; the price is that a local
shadowing a Cells' name for part of the body hides a real read later in it. None of the shipped
model's formulas does that. A client SHOULD say the map is read from the formulas, because a reader
will otherwise take a link for a measured dependency.

## 18. What modelx-mcp needs (0.10.0)

*Added 2026-10-05, bridge **0.10.0**. `protocol` stays `0`, no method is new, and nothing a 0.9.0
client sends gets a different answer. Each subsection names its `features` entry; detect each one
separately.*

modelx-mcp (PLAN §3.6, spike S6) is an MCP server whose tools read a model through
`Bridge.dispatch` and nothing else, so a model can ask about a calculation the way the panels do.
Writing it found seven places where the honest answer cost a client many round trips, a pile of
handles, or a guess. Each is fixed here at the source, as an OPTIONAL result field or an OPTIONAL
param whose default is the 0.9.0 behaviour. `python/modelx_bridge/tests/test_mcp_support.py` pins
every one on the shipped BasicTerm_S and on a synthetic model, and asserts that the defaults the
shipped frontend reads did not move.

| § | `features` entry | what | kind |
|---|---|---|---|
| 18.1 | `session.computed` | `session.info.models[].computed` | field |
| 18.2 | `stats.extremes` | `argmin`, `argmax`, `argmin_label`, `argmax_label` on a numeric `stats` block | field |
| 18.3 | `trace.values` | `values: false` on `trace.preds` / `trace.succs` | param |
| 18.4 | `cells.page.element` | `element` on `cells.page` | param |
| 18.5 | `table.get.label` | `label` on `table.get` | param |
| 18.6 | `error.display` | `error_display` on a `formula_error` | field |
| 18.7 | `handle.index_name` | `index_name` on a Series' or DataFrame's handle tag | field |

**A client MUST detect the three params before sending one.** `trace.*` and `table.get` silently
drop a param they do not know, so on a 0.9.0 kernel `table.get {label: 10}` answers row 0 as if it
were row 10, and `values: false` gets every value it asked not to receive, with a handle minted for
each vector. (`cells.page` refuses an unknown param, §13.9, so `element` fails loudly instead.)

Measured numbers below are from the shipped BasicTerm_S unless they name one of lifelib's larger
models — CashValue_ME, BasicTerm_ME, BasicTerm_SE — which spike S6 stages and the product does not
ship. modelx 0.33.0, pandas 3.0.6, CPython 3.11.

### 18.1 `computed` on `session.info.models[]` (`session.computed`)

```jsonc
{"type":"req","id":"c1","method":"session.info","params":{}}
{"type":"res","id":"c1","result":{"protocol":0,"bridge":"0.10.0", ...,
 "models":[{"name":"BasicTerm_S","revision":1,"path":null,"dirty":false,"sample":"BasicTerm_S",
            "computed":0}], ...}}
// the same entry after pv_net_cf() and Projection[2].pv_net_cf():
{"name":"BasicTerm_S","revision":4,"path":null,"dirty":false,"sample":"BasicTerm_S","computed":5465}
```

`computed` is how many nodes the model has computed: the size of modelx's tracegraph. **It is the
only cached-value total that counts inside ItemSpaces.** `tree.get`'s per-Cells `cached` covers the
named Spaces only (§15 does not recurse into ItemSpaces), so a client summing it misses everything an
ItemSpace computed. Measured: `0` fresh; `1832` after `pv_net_cf()`, equal to the tree's sum; `5465`
after `Projection[2].pv_net_cf()`, while the tree still sums to 1,832. That is 5,464 cached Cells
values — 3,632 of them inside `Projection[2]` — plus **one node for the ItemSpace itself**, which
the Space formula created. A Cells with `is_cached = False` adds nothing; a node that reads no other
node counts like any other. Reading it costs 0.45 µs.

It also moves when an evaluation **fails**: a failed calculation keeps what it computed before it
failed (`claims(t=9999)` after `pv_net_cf()`: +4). The revision does not move with it — §18.8.

`null` when the count cannot be read: `0` is the claim "nothing is computed", and a client may
print it as one.

### 18.2 Where the extremes are (`stats.extremes`)

```jsonc
// table.stats on disc_rate_ann under pandas 3 (under pandas 2 its labels are np tags; see below)
{"type":"req","id":"c3","method":"table.stats","params":{"h":"h1"}}
{"type":"res","id":"c3","result":{"h":"h1","kind":"Series","col":0,"name":"zero_spot",
 "dtype":"float64","total_rows":151,
 "stats":{"scope":"column","kind":"numeric","n":151,"count":151,"nulls":0,
          "sum":2.9656599999999997,"mean":0.019640132450331124,"min":0.0,"max":0.03056000000000001,
          "argmin":0,"argmax":150,"argmin_label":0,"argmax_label":150}}}
// cells.page {obj: "Projection.claims"} after pv_net_cf(), its stats block:
{"scope":"column","kind":"numeric","n":121,"count":121,"nulls":0,"sum":5814.680787725242,
 "mean":48.05521312169622,"min":0.0,"max":64.47847187567388,"argmin":120,"argmax":108,
 "argmin_label":{"$t":"np","dtype":"int64","v":120},"argmax_label":{"$t":"np","dtype":"int64","v":108}}
```

Every `numeric` and `integer` block (§13.8) — from `table.stats` and from `cells.page` alike — says
where its minimum and maximum are, because the question "what is the peak" always comes with "when".
`claims` rises and then falls, so neither extreme is at an end a reader would guess: the minimum is at
t=120 and the maximum at t=108.

- `argmin` / `argmax` are **positions** in the column, 0-based: the first minimum and the first
  maximum, nulls skipped. They are counted over the column **as given**, never over a null-dropped
  copy — measured on a masked `Int64` column `[10, 20, <NA>, 40]`, the maximum is position 3 where
  the null-dropped array says 2.
- `argmin_label` / `argmax_label` are the **index labels** at those positions, codec-encoded (§5):
  for a one-parameter Cells, the argument. They carry whatever pandas hands back, so a `RangeIndex`
  gives plain JSON integers and an `int64` index gives `np` tags, as above. They are `null` for an
  ndarray and for an Index, whose rows have positions only.
- **Which of the two a table has can depend on the pandas version, not on the bridge.** Measured
  2026-10-06 on the shipped BasicTerm_S: pandas 3.0.6 reads `disc_rate_ann`'s `year` and
  `model_point_table`'s `point_id` from their .xlsx as `RangeIndex`es, so the first example's labels
  are `0` and `150`; pandas 2.2.3 and 2.3.3 read the same files as `int64` indexes, so the same
  request answers `{"$t":"np","dtype":"int64","v":0}` and `{"$t":"np","dtype":"int64","v":150}`. A
  `RangeIndex` hands back a Python `int` under all three, so the bridge converts nothing, and a
  client MUST accept either form for an integer label.
- **A label never mints a handle.** One whose encoding would exceed `limits.max_inline_bytes`
  (8192), or that is itself a pandas or numpy container (an object Index can hold an ndarray), is an
  `opaque` tag carrying its repr instead. The first cut encoded labels like values, which promotes an
  oversized one to a handle (§5): measured, a two-parameter Cells keyed by two 4,500-character
  strings gave `argmax_label` as a `handle`, so `cells.page` minted one, against §13.2, and
  `table.stats` over a Series whose two labels were that size minted two. Neither tag can be sent
  back as an argument; a label under the limit can.
- When the column has no number to point at (`count` 0), all four are `null`, not `0`.
- A `text` or `other` block carries none of the four keys.
- The cost is two more passes over the column: measured at 200,000 rows, the whole block went from
  0.75 to 1.36 ms for float64 and from 7.7 to 7.9 ms for int64, whose exact sum dominates.

Verified against pandas: `model_point_table`'s `sum_assured` (10,000 rows) has its labels equal to
`idxmin()` / `idxmax()` and its positions one less, because `point_id` starts at 1.

### 18.3 Neighbours without values (`trace.values`)

```jsonc
{"type":"req","id":"c6","method":"trace.preds",
 "params":{"obj":"Projection.claims","args":[3],"evaluate":false,"values":false}}
{"type":"res","id":"c6","result":{"revision":4,"cached":true,
 "node":{"obj":"Projection.claims","args":[3],"display":"BasicTerm_S.Projection.claims(t=3)",
         "predslen":2,"succslen":1},
 "preds":[{"obj":"Projection.claim_pp","args":[3],"display":"BasicTerm_S.Projection.claim_pp(t=3)",
           "predslen":1,"succslen":1},
          {"obj":"Projection.pols_death","args":[3],
           "display":"BasicTerm_S.Projection.pols_death(t=3)","predslen":2,"succslen":3}]}}
```

`trace.preds` and `trace.succs` take an OPTIONAL boolean `values`, default `true` (the replies of
§6.6 and §16). With `false`, **no `value` key appears anywhere in the reply** — not on the node, not
on any neighbour, not as the `null` of an uncomputed node — and everything else is identical,
`predslen` and `succslen` included. A non-boolean is `bad_request`, **`null` included**, as
`evaluate: null` is in the same call; a client that wants the default leaves the key out. The first
cut read `null` as the default, so `values: null` answered with every value and minted a handle per
vector, the one answer a client sending it can least have meant. It applies to the evaluating
`trace.preds` too: the node is computed, and answered without values.

**The value is never encoded, which is the point.** Encoding a vector value mints a handle (§5), so a
trace of a node with many vector-valued neighbours fills the 64-entry LRU with handles nobody asked
for and evicts the ones a grid is paging. Encoding each value and deleting the key afterwards mints
them all the same; that was the first cut, and it was caught by counting the codec's handles, not the
reply's bytes. Measured:

| node | with values | `values: false` |
|---|---|---|
| BasicTerm_S `pv_claims()`, 123 precedents | 25,578 bytes, 1 handle (`disc_factors()`) | 15,454 bytes, 0 handles |
| CashValue_ME `pv_premiums()`, 1,143 precedents | 543,881 bytes, 1,143 handles, 300 ms | 149,977 bytes, 0 handles, 13 ms |
| CashValue_ME `model_point()`, 4,575 dependents | **refused**: 2,073,349 bytes, over `max_message_bytes`, after minting 4,576 handles (1,123 ms) | 634,002 bytes, 0 handles, 61 ms |

A client that wants some of the values reads those with `value.get` afterwards.

### 18.4 One element of each cached value (`cells.page.element`)

```jsonc
// Synth.S.v(t) = pd.Series([t*1.0, -t*2.0], index=pd.Index([1, 2], name="point")), t = 0..4 cached
{"type":"req","id":"c8","method":"cells.page","params":{"model":"Synth","obj":"S.v","element":2}}
{"type":"res","id":"c8","result":{"revision":1,"obj":"S.v","display":"Synth.S.v","params":["t"],
 "value_name":"v","value_dtype":"float64","scalar_values":true,"n_cached":5,"retains":true,
 "too_large":null,"focus_row":null,"keys":{"integer":true,"min":"0","max":"4","gaps":0},
 "stats":{"scope":"column","kind":"numeric","n":5,"count":5,"nulls":0,"sum":-20.0,"mean":-4.0,
          "min":-8.0,"max":0.0,"argmin":4,"argmax":0,
          "argmin_label":{"$t":"np","dtype":"int64","v":4},
          "argmax_label":{"$t":"np","dtype":"int64","v":0}},
 "page":{"kind":"DataFrame","shape":[5,2],"row":0,"rows":5,"col":0,"cols":2,"total_rows":5,
         "total_cols":2,"index":{"name":"","dtype":"int64","values":[0,1,2,3,4]},
         "columns":[{"name":"k0","dtype":"int64","values":[0,1,2,3,4]},
                    {"name":"v","dtype":"float64","values":[0.0,-2.0,-4.0,-6.0,-8.0]}],
         "format":"json","complete":true,"buffers":0},
 "element":2,"element_missing":0}}
```

For a Cells whose values are **Series** — every lifelib `_ME` model, where a value holds one number
per model point — `element` (a codec-encoded label) pages `value.loc[element]` of each cached value
instead of the values: one model point across the Cells' arguments, as an ordinary scalar column with
§13's `keys`, the whole-column `stats` and §18.2's extremes. Measured on CashValue_ME:
`margin_mortality`, element 1, is 121 values whose maximum, 23.19979903678737, is at t=87 — one
request, where without it each of the 121 values is a handle (§5) to page and reduce on the client,
against a 64-entry LRU.

- **It MUST NOT evaluate, and does not**: it is a pandas index lookup (`Index.get_loc`) on values
  already cached, never a modelx call, so §13.1 holds unchanged. It mints no handle (§13.2).
- `element` is echoed on **every** reply to a request that carried it. `element_missing` counts the
  cached values that do not carry the label — each is a NaN in the column, so `stats` leaves it out
  of `count` — and is `null` on a reply that read no series (a zero-parameter Cells' note, or
  `too_large`).
- **An empty cache is an empty column, not a refusal**: `n_cached` 0, `element_missing` 0. The
  first cut tested the values' dtype first and refused a Cells with nothing cached as "values are
  scalars", blaming the type for a run that had not happened.
- Refused with `bad_request`, by name: a Cells whose values are scalars (BasicTerm_S's `claims` is
  one); a value that is not a Series (a DataFrame row is not an element; an ndarray has no labels);
  a label that selects several rows of a value; a label that cannot be one, such as a JSON array.
- A **partial key** of a MultiIndex is refused by its own sentence, however many rows it selects,
  because what it selects is a sub-Series. It names the levels and asks for the full label, a `tuple`
  tag (§5), which works: `label 2 is a partial key of the value cached for 0, whose index has 2
  levels (pt, kind), so it selects 1 row of it, not one element; pass the full label, a tuple of 2`.
  The first cut read that case as "label 2 selects 1 rows ..., not one element".
- `element: null` is the request without it.

### 18.5 A page that starts at a label (`table.get.label`)

```jsonc
// h1 is Projection.disc_rate_ann, a 151-row Series indexed by year
{"type":"req","id":"c4","method":"table.get","params":{"h":"h1","label":10,"rows":2}}
{"type":"res","id":"c4","result":{"kind":"Series","shape":[151,1],"row":10,"rows":2,"col":0,
 "cols":1,"total_rows":151,"total_cols":1,"index":{"name":"year","dtype":"int64","values":[10,11]},
 "columns":[{"name":"zero_spot","dtype":"float64","values":[0.01188,0.01226]}],
 "format":"json","complete":false,"buffers":0,"h":"h1","focus_row":10,"label_matches":1}}
{"type":"req","id":"c5","method":"table.get","params":{"h":"h1","label":999}}
{"type":"res","id":"c5","error":{"code":"not_found","message":"no row labelled 999 in this Series",
 "data":{"kind":"Series"}}}
```

`label` (codec-encoded, in place of `row`) starts the page at the row carrying that index label, and
the reply gains `focus_row`, that row, and `label_matches`, how many rows carry the label (1 for a
label carried once, as above). It reads one element of a long Series by its label without
paging to it: on BasicTerm_ME `premiums(t=0)` is 10,000 rows over `policy_id` 1..10000, so label
7342 is row 7341 — off by one from the obvious guess, and on an index with gaps no guess works.
The lookup is pandas' `Index.get_loc`, never a scan.

- `label` with `row` is `bad_request`. A label not in the index is `not_found`. An ndarray or an
  Index has no labels: `bad_request`. A label that cannot be one (a JSON array) is `bad_request`.
- A label matching several rows — a repeated label, or the leading levels of a MultiIndex — lands on
  the **first** of them, and `label_matches` counts them all. **The others are on this page only
  when they are adjacent and fit in `rows`**, as on a sorted index. Scattered ones are not:
  measured on a 202-row Series indexed `[7, 1000..1199, 7]`, label 7 lands on row 0 with
  `label_matches: 2`, and the 100-row page holds one 7. The first cut said "the page shows the rest"
  and carried no count, so a client could not tell. The count comes from what get_loc returned (a
  slice's length, a mask's count); nothing is scanned again. A full MultiIndex label is a `tuple`
  tag (§5).
- Without `label`, or with `label: null`, the reply is exactly 0.9.0's: no `focus_row` and no
  `label_matches`.

### 18.6 The failing node, named (`error.display`)

```jsonc
{"type":"req","id":"c7","method":"value.get",
 "params":{"nodes":[{"obj":"Projection.claims","args":[9999]}]}}
{"type":"res","id":"c7","result":{"revision":4,"values":[{"ok":false,"error":{
 "code":"formula_error","message":"KeyError: np.int64(880)",
 "data":{"formula_traceback":"Error raised during formula execution\nKeyError: np.int64(880)\n...",
         "error_obj":"Projection.mort_rate","error_args":[9999],
         "error_display":"BasicTerm_S.Projection.mort_rate(t=9999)"}}}]}}
```

(`formula_traceback` cut here; it is modelx's own traceback, 781 characters in this run.)

`error_display` is the node that raised, **named** for a reader where `error_obj` / `error_args`
address it for a resolver (§3). `error_obj` is opaque (§4), and inside an ItemSpace it is modelx's
internal name: measured, a failure in `Projection[99999].pv_net_cf()` gives `error_obj`
`Projection.__Space2.model_point`, which names nothing a person or a model can cite, and
`error_display` `BasicTerm_S.Projection[99999].model_point()`. It is `null` when modelx kept no
traceback, because an empty string would read as a node. The evaluating `trace.preds` carries it too.

### 18.7 What a Series' or DataFrame's labels are (`handle.index_name`)

```jsonc
{"type":"req","id":"c2","method":"value.get",
 "params":{"nodes":[{"obj":"Projection.disc_rate_ann","args":[]}],"evaluate":false}}
{"type":"res","id":"c2","result":{"revision":4,"values":[{"ok":true,
 "display":"BasicTerm_S.Projection.disc_rate_ann","cached":true,"args":[],
 "value":{"$t":"handle","h":"h1","kind":"Series","dtype":"float64","shape":[151],
          "columns":["zero_spot"],"index_preview":[0,1,2,3,4,5,6,7,8,9],
          "preview":[[0.0],[0.00555],[0.006840000000000001],[0.00788],[0.00866],[0.00937],
                     [0.00997],[0.0105],[0.01098],[0.01144]],
          "repr":"year\n0      0.00000\n1      0.00555\n ... Name: zero_spot, Length: 151, dtype: float64",
          "index_name":"year"}}]}}
```

(`repr` cut here.) `index_name` says what the labels in `index_preview` are, so a reader can print
"max at year=150" from the tag alone instead of paging the index for a header. One string, clipped
like every preview string (120 characters):

- `"year"` for `disc_rate_ann`, `"point_id"` for `model_point_table`; on lifelib's CashValue_ME a
  value per model point says `"poind_id"`, lifelib's own spelling.
- A MultiIndex's level names joined with `", "` **in level order** — BasicTerm_SE's `premium_table`
  gives `"age_at_entry, policy_term"` — with an unnamed level written `None`, because dropping it
  would pair the remaining names with the wrong positions of a tuple label.
- `null` for an index with no name (`model_point()` in BasicTerm_S is a Series over column names).
- **Absent** on an ndarray's or an Index's tag. An ndarray has no index, and `null` would say "an
  unnamed one".

### 18.8 Considered and deferred: a revision bump on failure, and a canonical display

Two more changes were measured with these and **are not in 0.10.0**. Both change behaviour the
shipped UI relies on — when `model.changed` fires, and the display strings its panels show and
compare — which is exactly where this project's worst defects have hidden (CLAUDE.md), so each needs
a browser run of its own. Both are deferred to PLAN §5a, and `test_mcp_support.py` pins the
behaviour each would change, so landing one moves that suite on purpose.

**A failed evaluation does not move the revision.** Measured on 0.9.0 and unchanged here: on a fresh
BasicTerm_S, `value.get {evaluate: true}` on `claims(9999)` answers `formula_error` at revision 1 with
no event, but modelx has already computed 8 nodes on the way to the failure. The next `post_execute`
— which in the Pyodide kernel follows every comm message (§7) — sees the fingerprint move and reports
`{"revision": 2, "reason": "execute"}`, and `session.info` then says `dirty: true`: the bridge's own
evaluation is blamed on the user's code, and `model.close` refuses without `force`. The candidate
fix bumps with reason `evaluate` when the fingerprint moved, and re-raises. Until it lands,
`computed` (§18.1) is the signal that a failure left something behind.

**`value.get` displays the node as requested; `trace.*` displays it with defaults filled.** For a Cells
with a defaulted parameter, `value.get` at `args: [3]` displays `Synth.S.f(t=3)` and echoes `[3]`, while
the trace of a node that reads it lists `Synth.S.f(t=3, kind=None)` with `args: [3, null]`; on
CashValue_ME, `pv_claims()` and `pv_claims(kind=None)`. **They are one node**: measured, a `value.get`
at `[3, null]` reads the value `[3]` cached, and `trace.succs` answers the same node for either. The
candidate fix builds `display` from the node's own arguments while still echoing `args` as requested
(`nodebar.tsx`, `trace.tsx` and `formula.tsx` compare `entry.args` with what they sent). Until it
lands, a client that cites or matches nodes by display MUST treat both spellings as the same node;
modelx-mcp resolves both.

### 18.9 What is still not here

Found while writing modelx-mcp and left for later: `trace.*` has no `limit` / `offset`, so a node with
thousands of neighbours is one reply (634,002 bytes for 4,575, above, with `values: false`);
`value.get` has no `values: false`, so reading a range of vector values mints one handle per node; an
uncomputed `trace.*` node still carries `predslen: 0, succslen: 0` beside `cached: false`; a `None`
key reaches `cells.page` as NaN; and there is still no cancellation (§2).
