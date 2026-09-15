# A gate you record yourself is not a gate

Both human gates in this flow are `affirm`, which means the engine wants a person's answer AND a
witness that a person actually spoke. The witness counts human turns since the last gate. Record a
gate in the same turn you asked for it and it refuses with exit 3.

**That refusal is the whole mechanism, not an obstacle in front of it.** An agent that records its own
confirmation is an agent marking its own homework, and the log afterwards is indistinguishable from
one where a person really answered — which is precisely why the check has to happen at the moment of
recording rather than in review.

So the shape is: ask, **end the turn**, and record what they said when they say it. Two gates rather
than one is deliberate here: a witness that only ever sees a single gate cannot demonstrate that it
counts the turns BETWEEN them.

`--evidence` takes their words, not your summary. A summary turns "fine, but check X first" into
"fine".
