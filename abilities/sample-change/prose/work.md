# work — read the world, judge, and move the item

Three things happen here, and the middle one changes shape with the variant. What does not change is
that both of the claims made in this phase are checked against the world rather than accepted.

## Step: W01 Read what the world says about the item

> - **Output**: a `sample_read` row — `found` or `absent`.
> - **Complete**: the recorded value survives a check against `item_found`.

Recording `found` while the item is nowhere on disk is refused, and the refusal names the fact that
contradicted it. This is the cheapest possible demonstration of the pattern: the record is not the
evidence, the record is a CLAIM and the fact is what corroborates it.

`absent` is a legitimate answer. It is not the convenient one — the phase goal still needs a
judgement — but recording it honestly costs nothing.

## Step: W02quick Quick path — accept the item as it stands

> - **Applies under**: variant `quick`.
> - **Complete**: one `sample_judgement` row.

The quick path exists so the variant mechanism has something to exclude. Under `full` this step
reports "not part of this flow", and that refusal is correct rather than a bug.

## Step: W02full Full path — judge the item against the settled set

> - **Applies under**: variant `full`.
> - **Complete**: a `sample_judgement` row whose value is one of the declared three.

The declared set is what makes the outcome countable later. A free string here would let two runs
record the same decision in two words, and `harness history` could show them but not group them.

## Step: W04quick Quick path — record the judgement and move on

> - **Applies under**: variant `quick`. The SECOND step this variant owns.
> - **Complete**: one `sample_settled` row.

A variant that owns a single step makes the construct look like a coin flip. Owning a short sequence
is what it is actually for — and it is what lets the engine check that the shared core stays the
majority rather than the exception.

## Step: W04full Full path — record the judgement with its reason

> - **Applies under**: variant `full`. The second step this variant owns.
> - **Complete**: one `sample_settled` row.

Depends on `W02full`, within its own variant. A dependency that CROSSES variants is refused at load
time — `W03` cannot depend on `W04quick`, because under `full` that step does not exist and the run
could never close. What keeps both variant steps mandatory is the phase goal's `any_of`, not a
dependency: the goal names the pair, and whichever one applies must be closed.

## Step: D01 Optional — gather more before judging

> - **Optional**: yes. Skipping is RECORDED, not silent.
> - **Complete**: one `sample_research` row, or a recorded skip.

`harness skip --run <id> --step D01 --reason "<why>"`. A skip with a reason is a fact about the run;
a step quietly left open is an absence that looks the same as work in progress.

## Step: W03 Move the item to review

> - **Output**: the item, physically in `review/`.
> - **Complete**: the recorded status agrees with the directory the item is actually in.
> - **Guards**: `move_item` (see `I02`).

Move the file FIRST, then record the status. The other order earns a refusal that names the folder
the reader actually found — because the directory is the status in this layout, which is what makes a
claimed move checkable at all.
