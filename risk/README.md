# Fraud detection

Two rails, wired into the simulation through one injected protocol.

## How it attaches

`sim.core.interfaces` defines `RiskScorer`, `RiskContext` and `RiskDecision`.
The engine consults whatever is injected and never learns what implements it;
`sim.main._build_risk` is the only place the simulation constructs anything from
this package, and an import-linter contract enforces that.

Off by default. With `FINSIM_ENABLE_RISK` unset the engine emits exactly what it
emitted before this package existed, so every replay, determinism and state-hash
guarantee is untouched. Turn it on with `enable_risk = true` or
`FINSIM_ENABLE_RISK=1`.

```
Command -> engine validates ownership and funds -> RiskScorer.assess -> events
```

Risk is consulted **last**, after ownership and balance. A payment that was
going to fail anyway is never scored: it would teach a model that "declined for
no money" looks like fraud, and it would let an attacker probe the risk system
for free with payments they cannot fund.

A scorer that raises is treated as ALLOW. A risk model is advisory - if it
crashes, payments must keep clearing. An exception escaping into the command
pipeline would turn one bad model file into a total payments outage, which is a
worse failure than missing some fraud.

## The two rails are not symmetric

**card** (`PAYMENT`) - someone is spending money that is not theirs. The loss is
immediate and bounded by the transaction, and a wrong decision is cheap to
recover from: a genuine customer confirms with a code and carries on. This rail
may block.

**wire** (`TRANSFER`) - money is being moved to disguise where it came from.
Nothing is wrong with any single transfer; the pattern exists only across
accounts and over time. This rail **never stops a transfer**, for two reasons
that are both permanent:

- Detection runs at roughly 12% precision. Blocking automatically would refuse
  about eight innocent transfers for every real laundering leg.
- Telling someone they are under money-laundering review is **tipping off** - a
  criminal offence under India's PMLA, the US Bank Secrecy Act and the UK's
  Proceeds of Crime Act. A decline reading "flagged as suspicious" would commit
  that offence automatically, several times a day.

So the wire rail raises a case. A named human decides whether to freeze, and the
freeze is a separate action with its own record.

Everything else - cash in, cash out, fees, interest, settlement - is not scored.
None is a route an attacker controls end to end, and scoring them would add
false positives with no fraud behind them.

## What is here

| module | what it does |
| --- | --- |
| `engine.py` | routes by transaction type, counts outcomes, holds cases |
| `thresholds.py` | Elkan's rule - large payments face a lower bar |
| `card/history.py` | rolling per-account sequences built from the live stream |
| `card/scorer.py` | the seam a trained sequence model plugs into |
| `wire/graph.py` | account graph over a sliding window, cycles and fan-out |
| `wire/scorer.py` | structural signals to cases |
| `card/encoding.py` | the one place FinSim's fields map onto the model schema |
| `card/model.py` | load a checkpoint, score one account |
| `card/training.py` | pretrain, fine-tune, save |
| `card/treasure/` | the architecture, vendored from arXiv:2511.19693 |
| `card/warmstart.py` | copies the decoder body from a model trained elsewhere |
| `collect.py` | writes scored traffic to disk, exactly labelled |
| `actions.py` | freeze, clear, reopen - append-only, named reviewers |
| `monitoring.py` | PSI and flag-rate drift against a stored reference |
| `console.py` | a page a reviewer opens to see what was flagged |

## What is honest about the current state

**The card rail has no trained model.** `UntrainedCardScorer` allows everything
and logs a warning saying so. It is not a stand-in that approximates the model -
it does nothing at all, deliberately, because a hand-written substitute scoring
a few percent would be indistinguishable from a broken model at a glance and
would make this integration look finished when it is not.

The offline work established what to build: a sequence model reaching 33.2%
recall at a 2% flag rate using only fields a simulator can supply, against 14.1%
for a per-row model on the same fields. It also established the recipe - six
epochs of self-supervised pretraining, then twenty supervised. What it did not
produce is weights that can be copied here, because the offline model was given
card identifiers as inputs and a FinSim account id has no entry in that
embedding table.

**The wire rail is structural, not statistical.** "This account sits on two
cycles that closed within a day and forwarded 96% of what it received" is
something the graph either shows or does not. The weights in `WireThresholds`
are judgements, written down so they can be argued with, and nothing here claims
a precision it has not measured on this simulator's traffic.

## Getting a model in

Everything below is built and tested. Only the traffic is missing.

```bash
# 1. Collect. The card history is maintained even by the untrained scorer,
#    which is what makes it training data.
export FINSIM_ENABLE_RISK=1
export FINSIM_TRAFFIC_LOG=runs/traffic.jsonl
python -m sim.main run-seed

# 2. Train. Pretrains without labels, then fine-tunes on the labelled fraud.
python -m risk.card.training --traffic runs/traffic.jsonl --out models/card.pt
```

That is the whole handover. `sim.main` looks for `models/card.pt` on start-up
and wires `SequenceCardScorer` when it finds one, `UntrainedCardScorer` when it
does not - logging which at warning level, because a rail that allows everything
and a rail that finds nothing look identical in the output otherwise.

Labels are exact rather than inferred: every event carries `actor_id` and the
red agent's identity is known, so a transaction is fraud if and only if the
attacker made it.

## What the wire rail measures on real simulator traffic

Fitted on a clean run - legitimate transfers only, no attacks - and then
measured on a run with the scripted laundering patterns injected, using the bar
from the clean run and never re-fitted.

| | |
| --- | --- |
| false alarms on legitimate traffic | 0.53% |
| precision on the cases it raised | 28.6% |

Two bugs were found getting there, and both are worth knowing:

**The pass-through ratio was unbounded.** `sent / received` reads above 1.0 for
any account spending money that arrived from outside the transfer graph - a
salary, a deposit. A rule reading "at least 0.90" flagged **80% of legitimate
accounts**, with reasons like "forwarded 246% of what it received". A mule sits
just *below* one, so the signal needed a band, not a floor.

**The bar was a guess.** 0.60 was reasoned about rather than measured, and a
textbook mule chain - six accounts in, 93% straight back out to six others
within the hour - scored 0.35 and passed unflagged. `calibrate()` now fits it to
a target flag rate on clean traffic.

One thing the simulator does that is worth knowing: **its population never
completes a transfer.** `_propose_user_actions` gives transfers 30% of its
action weight but sets the target to a freshly generated UUID rather than an
existing account, so `_execute_transfer` finds no destination and returns before
emitting anything. The wire rail therefore sees no organic transfer traffic. That
is the simulator's own behaviour and this integration does not reach in and
change it.

## What the card model measures, and why the headline number is not one

Trained on 26,007 simulator transactions with 1,386 fraud (5.3%), three
configurations, three seeds each. The model loads, scores live traffic, and the
amount-aware bands fire: on a short run it blocked 9 payments and challenged 2
out of 418, a 2.6% flag rate.

**The recall figure it reports is an artefact and should not be quoted.** Every
one of the nine runs returned exactly 23.06% recall at a 2% flag rate, with zero
spread. That is not nine models agreeing; it is arithmetic. Precision was
**1.000 in every run** - every flagged transaction was fraud - and the holdout
holds 451 frauds in 5,202 rows. Flagging 2% means flagging 104 transactions, so
104/451 = 23.06% is the ceiling any model hits once it can rank 104 frauds at
the top. The operating point, not the model, is the binding constraint.

Two things follow, and both matter more than the number:

**The scripted fraud is too easy.** Perfect precision and an AUC reaching
0.9999 mean the patterns are trivially separable. They were written from the
red-team playbook, and a model that learns them has learned four specific
shapes rather than what fraud looks like.

**A 2% flag rate is the wrong operating point for this fixture.** The holdout is
8.7% fraud, four times the flag budget. Real card fraud runs near 1%; the
harness over-injected. Where the arms actually differ is at 5%, which ranges 38%
to 58%.

So the model is real, the pipeline is verified end to end, and **the number that
would tell you whether any of this works does not exist yet**. It comes from the
red team, attacking patterns the model has never seen.

One measured aside: pretraining did **not** help here. The no-pretrain arm took
the best AUC. On IEEE-CIS's 590,540 rows it was the only lever that paid; on
26,007 it does not, which is consistent with a self-supervised phase needing
volume.

## Findings from a review of this branch

A review of the diff found ten issues; all are fixed and each has a test. Four
were material:

**The supervised loss trained on labels that contradicted each other.**
`to_sequences` emits one sequence per transaction, so a fraudulent row reappears
as a non-final position in up to 31 later sequences - where it sat at a
placeholder zero. The loss read every real position, so the model saw that row
once labelled fraud and twenty-nine times labelled genuine. Serving reads one
position; the loss now does too, through an explicit `label_mask`. **The first
trained model was affected and has been retrained.**

**An instantaneous cycle was invisible.** `fastest_cycle_hours` used `0.0` as
both "no cycle found" and "closed in zero hours", and the signal gated on
`0 < fastest`. A round trip whose legs share a timestamp - the most suspicious
shape this rail can see, and not exotic in a discrete-event simulator - never
fired. Worse, the sentinel test could re-fire after a legitimate zero and let a
slower cycle be reported as the fastest.

**The velocity counters saturated below the window they serve.** The timestamp
deque held 512 entries against windows reaching a week, so an account
transacting once a minute pinned `txns_last_day` and `txns_last_week` to the
same 512 after eight hours - on exactly the card-testing bursts and mule
throughput those features exist to separate.

**The account graph did not bound its memory.** Eviction decremented the totals
but never removed a zeroed entry, so the dict grew for the life of the process
while the docstring claimed the window bounded it.

The rest: the eviction sweep ran on a modulus of a counter that unscored types
also advance (and those return before the sweep, so it could skip entire
intervals); `assess` documented a try/except it did not have, leaving the
counters unable to reconcile when a rail raised; the case list was unbounded;
`load()` dropped the checkpoint's stored model config, so a load-save round trip
stamped the default shape onto non-default weights; and two `dict(x or default)`
defaults where `{}` silently became the full default - the same class of bug as
the `AccountHistory` one, with a dict.

## What is still to do

1. Measure against the red team - the only test that is not marking its own
   homework.
2. Regenerate traffic with a realistic fraud rate and harder patterns, then
   refit the card bands against it.


## Two bugs worth knowing about

**`or` on a container that defines `__len__`.** `self.history = history or
AccountHistory()` silently discarded the caller's object, because an empty
history is falsy. The engine and its card scorer then kept separate histories,
the scorer's sequences never reached the recorder, and the collected training
set came out empty - with no error anywhere. Every such construction in this
package now uses `is None`, and a test pins it.

**Training and serving building features differently.** The classic silent
model failure: no exception, no obviously wrong number, just worse predictions
that look like a weak model. The defence here is structural - `card/encoding.py`
is the only implementation of the mapping, imported by both paths, and a test
asserts that what the recorder writes is what training reads.
