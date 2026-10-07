# modelx-bridge and modelx-mcp

Two Python packages for reading [modelx](https://github.com/fumitoh/modelx) models from outside
the code that built them.

- **[modelx-bridge](modelx-bridge/)** is a transport-agnostic JSON protocol over modelx. One
  `Bridge` object answers requests about the models open in its Python session (their tree,
  formulas, docstrings, values, traces and tables) and reports when a model changes. It ships with
  a Jupyter comm adapter.
- **[modelx-mcp](modelx-mcp/)** is an [MCP](https://modelcontextprotocol.io) server over the
  bridge. It lets an AI client such as Claude Code or Claude Desktop read a live modelx session:
  its formulas, its documentation, what is computed, and what each value was computed from. It is
  read-only by default, and one tool, `calculate`, computes.

modelx-mcp is for people who build models with modelx or [lifelib](https://lifelib.io) and want an
AI assistant that reads the model itself rather than guessing at it. modelx-bridge is for anyone
building a client on modelx: a notebook extension, a UI, another server.

The wire protocol is [docs/bridge-protocol-v0.md](docs/bridge-protocol-v0.md).

## Requirements

Python 3.10 or later, modelx 0.33, pandas 2.2.2 or later (pandas 3 included), numpy 2.0 or later
and openpyxl 3.1.5 or later. modelx-mcp also needs the MCP Python SDK, 1.10.0 or later and below 2.

Measured on Linux x86-64 by installing the built wheels into fresh environments and running both
packages' suites there, with no check failing: Python 3.10, 3.11, 3.12, 3.13 and 3.14; pandas
2.2.2, 2.2.3, 2.3.3, 3.0.0 and 3.0.6; numpy 2.0.0, 2.1.0, 2.2.6, 2.4.6 and 2.5.3; mcp 1.10.0 and
1.30.0. Each package's `pyproject.toml` says which combinations those were and what failed below
each floor. Windows, macOS and Python 3.15 have not been measured.

## Quick start: modelx-mcp

```bash
python -m pip install modelx-mcp
modelx-mcp --version
```

**Claude Code**, with that environment active:

```bash
claude mcp add modelx -- modelx-mcp --sample BasicTerm_S
claude mcp list
```

`BasicTerm_S` is a sample from lifelib that ships with modelx-bridge. For your own model, saved
by modelx as a folder or a zip:

```bash
claude mcp add my-model -- modelx-mcp --storage-root /path/to/models --open /MyModel
```

**Claude Desktop**: in `claude_desktop_config.json`, with the full path to `modelx-mcp`:

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

[modelx-mcp/README.md](modelx-mcp/README.md) lists the tools and every flag.

## Security

A modelx model is code. Its formulas are Python that runs when a value is computed, and opening a
model unpickles its data, which can run code of its own: modelx's unpickler does not restrict what
a pickle may do. **Open only models you trust**, through either package, exactly as you would only
run Python you trust.

Opening the shipped sample (`modelx-mcp --sample`, the bridge's `model.open_sample`) reads the
copy inside the installed `modelx-bridge` package, or the one in the folder `MODELX_BRIDGE_MODELS`
names when that is set, and never one from the working directory or any other folder. modelx-mcp's `run_python` tool runs
whatever code the client sends, so it exists only when the server is started with
`--allow-python`.

## Status

0.x: the protocol may still change between minor versions. The bridge relies on modelx's private
API, so each release is pinned to one modelx minor series (0.33 today). Both packages are built for
and used by lifelib Studio, whose demo is at [demo.lifelib.ai](https://demo.lifelib.ai/).

Some files here are maintained in lifelib Studio's own repository and copied here: the packages
`modelx-bridge/modelx_bridge` (the sample model and its licence included) and
`modelx-mcp/modelx_mcp`, `modelx-mcp/README.md`, and `docs/bridge-protocol-v0.md`. An accepted
change to one of them is made there and arrives here with the next copy. The rest (this README,
the packaging, the workflows) is maintained here.

Comments in the copied files cite lifelib Studio's documents and files, which are not published:
its plan and UI design, its spikes and their results, its frontend code, its `CLAUDE.md`, its AI
evaluation tasks under `evals/`, and modelx-mcp's design spec, cited as "spec N" for its section
N. The citations are kept because they record why a decision was made and what was measured to
make it; the measurement itself is in the comment.

## Repository layout

| path | what |
|---|---|
| `modelx-bridge/` | the `modelx-bridge` distribution: `modelx_bridge/`, its suites in `modelx_bridge/tests/`, and the sample model in `modelx_bridge/models/` |
| `modelx-mcp/` | the `modelx-mcp` distribution: `modelx_mcp/` and its suites in `modelx_mcp/tests/` |
| `docs/bridge-protocol-v0.md` | the protocol, normative |
| `.github/workflows/` | `ci.yml` builds both distributions and runs both suites against the installed wheels on every push and pull request; `release.yml` publishes one package to PyPI from a tag |
| `.github/check_dists.py` | what both workflows run on the built distributions: versions equal the code's, and every file is one meant to ship |
| `.github/jupyter_roundtrip.py` | what CI runs to drive `register_comm()` through a real Jupyter kernel |

The directories are hyphenated on purpose: a root directory named `mcp` would shadow the MCP SDK
whenever Python runs from the root.

## Checks

```bash
python -m pip install -e ./modelx-bridge -e ./modelx-mcp
python modelx-bridge/modelx_bridge/tests/run_all.py
python modelx-mcp/modelx_mcp/tests/run_all.py
```

Each `run_all.py` runs every suite of its package in a process of its own. It prints a table with
one line per suite (checks passed, failed and skipped, and the exit status), then each skipped
check with its reason, and exits 1 if any check failed. `-v` also prints each suite's own output,
one line per check: `ok`, `FAIL`, or `skip` with the reason. One suite alone runs as a module,
for example `python -m modelx_bridge.tests.test_doc`, and prints the same lines.

In this checkout `test_model` skips two checks: one reads a file that only lifelib Studio's
repository holds, the other a lifelib checkout beside this one. `test_lifelib` needs lifelib's own
models and otherwise reports itself SKIPPED
([modelx-mcp/README.md](modelx-mcp/README.md#checks)); CI's `lifelib` job runs it.

## Licence

BSD 3-Clause ([LICENSE](LICENSE)). The sample model `BasicTerm_S` is lifelib's, under the MIT
licence in
[modelx-bridge/modelx_bridge/models/LICENSE-lifelib.txt](modelx-bridge/modelx_bridge/models/LICENSE-lifelib.txt).
modelx itself is LGPL-3.0 and is installed as an ordinary dependency.
