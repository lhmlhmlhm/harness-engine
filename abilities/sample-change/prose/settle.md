# settle — one outcome, then close with nothing open

The last phase demonstrates two things a flow needs and a minimal example never shows: a second human
gate in the same run, and an exclusive pair where taking one step retires the other.

## Step: Z01 🚦 Settle the item (gate)

> - **Gate**: `affirm` — the second one in this run.
> - **Complete**: `gate_recorded`.

Same discipline as `I02`, and the same topic applies. The reason there are two gates rather than one
is in that topic: a witness that sees a single gate cannot show that it counts turns between gates.

## Step: Z02a Settle by accepting

> - **Exclusive with**: `Z02b`.
> - **Complete**: one `sample_outcome` row reading `accepted`.

## Step: Z02b Settle by withdrawing

> - **Exclusive with**: `Z02a`.
> - **Complete**: one `sample_outcome` row reading `withdrawn`.

Taking either of these retires the other. That is how a flow says "exactly one of these happened"
without leaving the road not taken open forever — and without the reader having to guess whether an
unclosed step means "not applicable" or "forgotten".

## Step: Z03 Close out with nothing open

> - **Gate**: `preauth:autosettle` — a config key may stand in for the person.
> - **Complete**: no violation is still open.

Preauthorised because closing out with nothing open is not a decision anybody needs to make twice.
`harness config --run <id> --set autosettle=true`.

If an obligation is still undischarged, this step refuses and names it. An obligation raised from a
derived fact is the one thing in this flow that can refuse a close on the world's word rather than
yours.
