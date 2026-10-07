# modelx-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI client such as Claude Code or
Claude Desktop read a live [modelx](https://github.com/fumitoh/modelx) session: its formulas, its
documentation, what is computed, and what each value was computed from.

It runs [modelx-bridge](https://pypi.org/project/modelx-bridge/) in-process, the same `Bridge` the
[lifelib Studio](https://demo.lifelib.ai/) frontend talks to, so every answer goes through the
bridge's wire protocol
([bridge-protocol-v0.md](https://github.com/fumitoh/modelx-bridge/blob/main/docs/bridge-protocol-v0.md)).
The tools see nothing but a `dispatch(method, params)` callable.

**Read-only by default.** No tool edits a formula or a Reference. One tool computes: `calculate`.
Every other tool reads with `evaluate: false` and prints `NOT COMPUTED` (unknown, not zero) for a
node with no value; the test suites check that around every reader call.

Version 0.1. Python 3.10 or later.

## Install

```bash
python -m pip install modelx-mcp
modelx-mcp --version
```

This installs `modelx-bridge` and modelx with it. The bridge ships one sample model, lifelib's
`BasicTerm_S`, which the server opens when it is given no model.

## Launch

```bash
modelx-mcp --sample BasicTerm_S
modelx-mcp --storage-root /path/to/models --open /MyModel
```

The server speaks MCP over stdio, so it is started by a client, not by hand.
`python -m modelx_mcp` is the same program, and `modelx-mcp --help` lists the flags.

| flag | what | default |
|---|---|---|
| `--sample ID` | open a shipped sample (repeatable) | `BasicTerm_S` when neither `--sample` nor `--open` is given |
| `--open PATH` | open a model folder or zip by its path under the storage root, e.g. `/MyModel` (repeatable) | |
| `--storage-root DIR` | the folder that holds your saved models; every `--open` path is under it, and `--open` without it is a usage error | none |
| `--allow-python` | add `run_python`, which is **not** read-only | off |
| `--max-chars N` | the bound on every tool output, at least 2,000 | 12,000 |
| `--align-cells NAME` | the Cells whose rows an ndarray's positions are matched to; `''` turns it off | `model_point` |
| `--log FILE` | append one JSON line per tool call the server runs: its arguments, every bridge call with its result, the text. A call the MCP SDK rejects on its arguments, before the tool runs, writes no line | off |
| `--print-tools` | print the `tools/list` JSON and exit, opening no model | |

Models open at launch and only at launch, so a ref means the same node for the server's life.
The server's instructions list them, and any that failed to open with the reason; stderr says the
same, in one line per failure and one line naming what opened.

stdout is the MCP channel and nothing else is written to it, at the file-descriptor level: at
launch the transport takes private copies of fds 0 and 1, fd 0 becomes the null device and fd 1
the server's stderr. So whatever a formula writes (`print`, fd 1, `sys.__stdout__`, a subprocess)
goes to stderr, or into `run_python`'s output while that runs, and `exit()` or `input()` cannot
reach the protocol's input.

### Claude Code

With the environment that has `modelx-mcp` active (its `bin` or `Scripts` folder on `PATH`):

```bash
claude mcp add modelx -- modelx-mcp --sample BasicTerm_S
claude mcp list
```

`claude mcp list` should report `modelx` as connected. Otherwise give `modelx-mcp`'s full path,
which `which modelx-mcp` (macOS, Linux) or `where modelx-mcp` (Windows) prints.

Your own model, saved by modelx as a folder or a zip under some directory:

```bash
claude mcp add my-model -- modelx-mcp --storage-root /path/to/models --open /MyModel
```

lifelib's library models are a quick way to try larger ones. `lifelib.create` writes a library
out as model folders:

```bash
python -m pip install lifelib
python -c "import lifelib; lifelib.create('basiclife', 'basiclife')"
claude mcp add modelx-lifelib -- modelx-mcp --storage-root "$PWD/basiclife" --open /BasicTerm_ME
```

Each server needs a name of its own: a second `claude mcp add modelx` in the same scope fails with
"MCP server modelx already exists in local config".

### Claude Desktop

Add the server to `claude_desktop_config.json`, with the full path to `modelx-mcp`:

```json
{
  "mcpServers": {
    "modelx": {
      "command": "/path/to/venv/bin/modelx-mcp",
      "args": ["--sample", "BasicTerm_S"]
    }
  }
}
```

On Windows the command is the path to `modelx-mcp.exe` in the environment's `Scripts` folder,
with each backslash doubled. This entry has not yet been run from Claude Desktop itself; the same
command and arguments were checked with the MCP client SDK over stdio.

## Security

A modelx model is Python code plus a pickle. Its formulas run whenever `calculate` computes, and
opening the model unpickles its data, which can run code of its own: modelx's unpickler does not
restrict what a pickle may do. Open only models you trust. `--sample
BasicTerm_S` opens the copy inside the installed `modelx-bridge` package, or the one in the folder
`MODELX_BRIDGE_MODELS` names when that variable is set, and never one from the working directory.
`run_python` runs whatever code the client sends, in the server's process and with your
permissions, which is why it exists only with `--allow-python`.

## The tools

Every tool names things with one string written as Python, the way the bridge displays it:
`BasicTerm_S.Projection.claims(t=3)`, `BasicTerm_S.Projection[2].pv_net_cf()`,
`BasicTerm_S.Projection.disc_rate_ann`. What a tool prints, a tool accepts.

- `get_tree`: Spaces, Cells with how many values each has cached, References, ItemSpaces. `filter=` matches Cells and Reference names only.
- `get_formulas`: formula source and docstrings, a Space's docstring in 8,000-character pages, a Reference's value and the paragraph of its Space's docstring that documents it.
- `get_map`: which Cells each formula names, read from source; one Cells' inputs and dependents level by level.
- `calculate`: **the only tool that computes.** Up to 50 refs and 200 nodes; `range(a, b)` in one argument; `+ - * /` over refs; creates an ItemSpace a ref needs. Labels each value `[computed now]` or `[cached]`.
- `get_value`: values already computed, never computing; a Cells' whole cached column with statistics; `label=` for one model point across t; `.loc[label]`, `.iloc[i]`, `[i]` (an ndarray only: on a Series it is ambiguous and refused), `["col"]`; paging.
- `trace`: what modelx recorded when a node was computed, grouped, with values, beside what the formula names; depth 1 to 3.
- `run_python` (only with `--allow-python`): Python in the server's interpreter against the same models, with the bridge's own report of what changed. Its output is everything the code wrote, in order: `print`, stderr, warnings, and what a subprocess wrote to fds 1 and 2; within `--max-chars`. It has no stdin: `input()` raises `EOFError`, and `exit()` is reported as `SystemExit`.

`modelx-mcp --print-tools` prints the tool descriptions and schemas exactly as a client receives
them; `--print-tools --allow-python` includes `run_python`.

`verify_citations(tools, claims)` in `modelx_mcp.tools` re-checks `ref = number` claims against
the cache without computing. It is a function for graders, not a tool.

## What 0.1 leaves out

| left out | why |
|---|---|
| editing (`formula.set`, `ref.set`) | editing is planned as approve-the-diff, with an impact preview and a journal |
| open, close, save, export | a model set fixed at launch keeps every printed ref meaning one thing |
| a `check` tool | a model-facing MATCH verdict passed a misaddressed citation; `verify_citations` is the function |
| one display per node | one node can print as `pv_claims()` and as `pv_claims(kind=None)`; both resolve to it ([protocol §18.8](https://github.com/fumitoh/modelx-bridge/blob/main/docs/bridge-protocol-v0.md#188-considered-and-deferred-a-revision-bump-on-failure-and-a-canonical-display)) |
| paging and cancellation in the bridge | `trace` in the bridge has no `limit`, and nothing cancels a long call ([protocol §18.9](https://github.com/fumitoh/modelx-bridge/blob/main/docs/bridge-protocol-v0.md#189-what-is-still-not-here)); for a node with more than 5,000 recorded neighbours the `trace` tool gives the count and points at `get_map` |
| async tools | sync tools match modelx's one thread; 331 evaluation runs through Claude Code saw no client timeout, the longest run taking 173 s |

## Checks

The suites are in the source distribution and the repository, not in the wheel. From a checkout
of [fumitoh/modelx-bridge](https://github.com/fumitoh/modelx-bridge):

```bash
python -m pip install -e ./modelx-bridge -e ./modelx-mcp
python modelx-mcp/modelx_mcp/tests/run_all.py
```

Each suite runs in its own process, and `test_stdio` drives the server over real stdio with the
MCP client SDK. `tools.json` is `--print-tools`' output, and `test_stdio` holds the two equal.
`test_lifelib` needs `MODELX_MCP_MODELS` set to a folder holding lifelib 0.17.1's `BasicTerm_ME`
and `BasicTerm_SE` (its basiclife library) and `CashValue_ME` (savings); without it, it reports
itself SKIPPED. Its computed values are compared to 1e-9 relative, because their last digits
depend on the CPU: numpy's `exp`, `log` and `power` round differently with and without AVX-512.

## Licence

BSD 3-Clause. modelx, which this runs, is LGPL-3.0.
