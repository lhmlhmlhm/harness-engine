# intake — name the item, then agree it is worth moving

The first phase does two things and no more: it records WHICH item this run is about, and it gets a
person to agree the item is worth touching. Everything downstream reads the first and is guarded by
the second.

## Step: I01 Name the item

> - **Output**: one `sample_item` row naming the item.
> - **Complete**: that row exists.

The borrowed fact (`sample-lib.sample_state`) resolves the item from THIS run's own evidence, not
from a parameter. So until this row exists, every later criterion that consults the world has nothing
to consult — and it will say so rather than pass.

Record the item's name, not a path: the reader looks the name up wherever it now lives, because
moving it between folders is the effect the later steps are checked against.

## Step: I02 🚦 Agree the item is worth moving (gate)

> - **Gate**: `affirm` — a person answers, and a witness backs it.
> - **Complete**: `gate_recorded`.
> - **Guards**: `move_item` — a `mv` out of `intake/` is refused until this gate exists.

Show the person what the item is, then **end the turn**. See topic `a-gate-you-record-yourself` for
why recording this yourself in the same turn is refused.

This gate also protects an action rather than only a decision: the runtime hook recognises the move
in a tool call and refuses it before it happens. You do not have to volunteer to ask.
