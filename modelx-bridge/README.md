# modelx-bridge

A transport-agnostic JSON protocol over [modelx](https://github.com/fumitoh/modelx): the kernel
side of lifelib Studio.

One `Bridge` object answers requests about the modelx models open in its Python session (their
tree, formulas, docstrings, values, traces and tables) with JSON-safe results, and reports when a
model changes. It knows nothing about transports. It ships with a Jupyter comm adapter, and
[modelx-mcp](https://pypi.org/project/modelx-mcp/) runs it in-process behind an MCP server.

The wire protocol is
[docs/bridge-protocol-v0.md](https://github.com/fumitoh/modelx-bridge/blob/main/docs/bridge-protocol-v0.md).

## Install

```bash
python -m pip install modelx-bridge
python -m pip install "modelx-bridge[jupyter]"    # to serve a Jupyter frontend from a kernel
```

Python 3.10 or later, modelx 0.33, pandas 2.2.2 or later (pandas 3 included), numpy 2.0 or
later, openpyxl 3.1.5 or later. Measured on Linux x86-64 with Python 3.10 to 3.14, pandas 2.2.2 to
3.0.6 and numpy 2.0.0 to 2.5.3;
[pyproject.toml](https://github.com/fumitoh/modelx-bridge/blob/main/modelx-bridge/pyproject.toml)
says what failed below each floor.

pandas 2 and pandas 3 read some integer indexes differently (pandas 3 makes a `RangeIndex` where
pandas 2 makes an int64 `Index`), so a label that arrives as a plain JSON integer under pandas 3
can arrive as a numpy-tagged integer under pandas 2. Protocol section 18.2 says a client accepts
either.

## Use it in-process

```python
from modelx_bridge import Bridge

bridge = Bridge()
print(bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"}))

result = bridge.dispatch("value.get", {
    "model": "BasicTerm_S",
    "nodes": [{"obj": "Projection.pv_net_cf", "args": []},
              {"obj": "Projection.claims", "args": [0]}],
})
for entry in result["values"]:
    print(entry["display"], entry["value"])
```

prints

```
{'model': 'BasicTerm_S', 'revision': 1, 'sample': 'BasicTerm_S', 'path': None, 'dirty': False}
BasicTerm_S.Projection.pv_net_cf() {'$t': 'np', 'dtype': 'float64', 'v': 910.92066093366}
BasicTerm_S.Projection.claims(t=0) {'$t': 'np', 'dtype': 'float64', 'v': 34.18079328868595}
```

`dispatch(method, params)` returns a result or raises `BridgeError`. `handle(message)` takes a
whole request envelope, `{"type": "req", "id": "...", "method": ..., "params": ...}`, and returns
exactly one response envelope; it never raises. The `id` must be a string. A message that is not a
request, or whose `id` is not a string, gets `None`, since a response would have nothing to be
matched to.

The methods: `session.info`, `model.open_sample`, `model.open`, `model.close`, `model.save`,
`model.export_zip`, `model.import_zip`, `storage.info`, `files.list`, `tree.get`, `formula.get`,
`formula.set`, `ref.set`, `doc.get`, `value.get`, `trace.preds`, `trace.succs`, `map.get`,
`table.get`, `table.stats` and `cells.page`. A client detects what a bridge supports from the
`features` list in `session.info`, not from its version string.

## Serve a Jupyter frontend

In the kernel:

```python
import modelx_bridge
modelx_bridge.register_comm()
```

The frontend opens a comm on the target `modelx-bridge` and receives a `hello` with the
`session.info` payload, then sends requests and receives responses and `model.changed` events on
that comm (protocol sections 1, 2 and 7). It needs ipykernel 6.19.1 or later, which the `jupyter`
extra installs.

## The sample model

`BasicTerm_S` from [lifelib](https://lifelib.io) 0.17.1 ships in
`modelx_bridge/models/`, with lifelib's MIT licence beside it, so `model.open_sample` works with
nothing else installed. Set `MODELX_BRIDGE_MODELS` to a folder holding a `BasicTerm_S/` folder to
use another copy.

## Security

A modelx model is code: its formulas run when a value is computed, and opening a model unpickles
its data, which can run code of its own (modelx's unpickler does not restrict what a pickle may
do). Open only models you trust. `model.open_sample` looks for the sample inside the environment
this package is installed in, or in the folder `MODELX_BRIDGE_MODELS` names, and never in the
working directory.

## Status

0.x. The protocol may still change between minor versions, and the bridge relies on modelx's
private API, so each release is pinned to one modelx minor series. It is built for and used by
lifelib Studio.

## Licence

BSD 3-Clause. The sample model in `modelx_bridge/models/BasicTerm_S` is lifelib's, under the MIT
licence in `modelx_bridge/models/LICENSE-lifelib.txt`. modelx itself is LGPL-3.0.
