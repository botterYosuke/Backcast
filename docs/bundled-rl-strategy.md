# Bundled RL strategy

Backcast ships one fixed reinforcement-learning policy, **RL Scalp Evolution v1**.
It is the default strategy and runs entirely from files in this repository. The
runtime does not import or read `_moomoo`.

## Use

1. Load a tick session.
2. Open the strategy workspace and leave **Bundled RL** selected.
3. Select **Load** to prepare and validate the immutable proposal trace.
4. Select **Start**, then play the replay.

Load is inert with respect to the broker: it does not move the replay cursor,
submit an order, create a fill, or change the portfolio. Start admits the run.
A proposal observed at tick `i` can first fill at tick `i + 1`.

The policy targets either 0 or 100 shares. Each proposal contains one result
for an actually flat portfolio and one for an actual 100-share portfolio.
Backcast selects the branch only after the previous broker commit. Any other
position, a non-empty order book, or an incomplete open-order snapshot stops
the run without guessing.

## Boundary

Python computes proposals before replay. The browser does not send source code
to this service, and replay dispatch performs no strategy HTTP calls after the
trace has been retrieved. Backcast's paper broker remains authoritative for
orders, fills, cash, position, and P&L. This reproduces the model decision
logic, not `_moomoo`'s separate fill simulator.

**Custom Python** is a separate, explicit choice. It displays the editor and
runs user-authored code in browser Pyodide. Switching strategies invalidates
the previously loaded runtime; the two cannot be mixed in one run.

## Provenance

The checkpoint was copied from
`_moomoo/models/rl_scalp_evolution_v1.json` as an authorized local project
asset. Its expected SHA-256 is
`528b9dc44928984c471731bb64ef82fa4a299a2ffd5707d1ce2a482223d28ef1`.
Public redistribution rights have not been established and are outside this
local-copy implementation.
