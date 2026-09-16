# work — read the world, judge, and move the item

Three things happen here, and the middle one changes shape with the variant. What does not change is
that both of the claims made in this phase are checked against the world rather than accepted.

This phase declares three **stages** — `read`, `judge`, `move`. A stage is not a phase: a phase
carries an acceptance criterion the engine checks, a stage carries nothing. It exists so a long phase
can be read and discussed in parts. Splitting this into three phases instead would have invented two
more acceptance gates nobody asked for, each needing a goal to satisfy.

## Step: W01 Read what the world says about the item

> - **Stage**: `read`.
> - **Output**: a `sample_read` row — `found` or `absent`.
> - **Complete**: the recorded value survives a check against `item_found`.
> - **Repeatable**: yes, budget 3.

Repeatable because reading the world again after you have CHANGED it is the same step, not a new one.
This step's claim is checked against the world, so a run that moves the item and re-reads is behaving
correctly rather than retrying a failure.

The budget is what keeps that honest. Unlimited attempts on a corroborated claim would let a driver
record every legal value in turn until one happened to agree — brute force wearing the shape of
diligence. An exhausted budget refuses, unconditionally.

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

> - **Stage**: `move`.
> - **Output**: the item, physically in `review/`.
> - **Complete**: the recorded status agrees with the directory the item is actually in.
> - **Guards**: `move_item` (see `I02`).
> - **Produced by**: `tools/sample-move.py`.

`produced_by` names the tool that CAUSES this step's effect, and the engine never runs it — it is a
pointer, not a hook. The contrast with the reading tool is the thing worth carrying away:

| | who calls it | why |
|---|---|---|
| `sample-lib/tools/sample-read.py` | the **engine**, through a fact provider | pure compute, so evaluating a criterion twice gives the same answer |
| `sample-change/tools/sample-move.py` | **you** | it moves a file, so calling it twice is not the same as calling it once |

A flow that pointed `produced_by` at the observing tool, or that let a provider call the effecting
one, would have crossed that line — and the symptom would be a criterion whose answer depends on how
many times it was checked.

Move the file FIRST, then record the status. The other order earns a refusal that names the folder
the reader actually found — because the directory is the status in this layout, which is what makes a
claimed move checkable at all.
