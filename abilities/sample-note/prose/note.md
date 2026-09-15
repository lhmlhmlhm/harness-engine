# note — record something that already happened

Two phases, four steps, and one thing to notice: this flow's scope is an IDENTIFIER, not a directory.
The runtime cannot tell that an action belongs to this run by where it was made, so the guard finds
the scope key inside the action's payload instead. Everything else here is ordinary.

## Step: N01 Say what happened

> - **Output**: one `sample_note` row.
> - **Complete**: that row exists.

Record what happened, not what you plan to do about it. This shape is for after the fact; a plan for
something not yet done is a different flow.

## Step: N02 🚦 Agree the note is right (gate)

> - **Gate**: `affirm`.
> - **Complete**: `gate_recorded`.
> - **Guards**: `publish_note` — refused at the tool call until this gate exists.

Ask, then end the turn. Recording your own confirmation in the same turn is refused by the witness,
and that refusal is the mechanism rather than an obstacle in front of it.

## Step: N02full Full note — say what it changes

> - **Applies under**: variant `full`, passed with `--variant`.
> - **Complete**: one `sample_impact` row.

This flow's variant is **explicit**, unlike `sample-change`'s, which is derived from a fact. The rule
is worth stating because both ship: derive when the WORLD knows (the scope, the directory, the branch),
ask when only the AUTHOR knows. Deriving what only the author knows produces a value that silently
disagrees with the intent; asking what the world already knows produces drift the moment the two
diverge.

Nothing on disk can tell a brief note from a full one, so this one asks.

## Step: N03keep Keep the note

> - **Exclusive with**: `N03drop`.
> - **Complete**: one `sample_verdict` row reading `kept`.

## Step: N03drop Drop the note

> - **Exclusive with**: `N03keep`.
> - **Complete**: one `sample_verdict` row reading `dropped`.

Dropping is a legitimate outcome and it is RECORDED. A note quietly not written is an absence that
looks the same as work still in progress.
