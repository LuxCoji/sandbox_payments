# Fraud detection

Two rails watching the simulation, wired in through one injected protocol.
`risk/README.md` covers the code; this covers the design decisions and what has
been measured.

## How it attaches

`sim.core.interfaces` defines `RiskScorer`, `RiskContext` and `RiskDecision`.
The engine consults whatever is injected and never learns what implements it.
`sim.main._build_risk` is the only place the simulation constructs anything from
`risk`, and an import-linter contract enforces that `sim` never imports it
otherwise.

```
Command -> validate ownership -> validate funds -> RiskScorer.assess -> events
```

Risk is consulted **last**. A payment that was going to fail anyway is never
scored: it would teach a model that "declined for no money" looks like fraud,
and it would let an attacker probe the rails for free with payments they cannot
fund.

A scorer that raises is treated as ALLOW. A risk model is advisory - if it
crashes, payments must keep clearing, because one bad model file becoming a
total payments outage is a worse failure than missing some fraud.

## Off by default, except for the red team

`enable_risk` defaults to `False`, so the engine emits exactly what it emitted
before this package existed and every replay, determinism and state-hash
guarantee is untouched.

`scripts/red_team_run.py` inverts that default, and the asymmetry is the point.
An attack session against a simulator with the controls switched off measures
nothing - every "nothing flagged it" finding it produces is true and
meaningless. That is what was happening before: the red-team path called
`build_simulation`, which read the global default, and nothing turned it on.
Pass `--no-risk` to run undefended deliberately.

## The two rails are not symmetric

**card** (`PAYMENT`) may block a payment. The loss is immediate and bounded by
the transaction, and a wrong decision is cheap to recover from - a genuine
customer confirms with a code and carries on.

**wire** (`TRANSFER`, `CASH_OUT`) never stops anything. Two reasons, both
permanent:

- Laundering detection runs at roughly 12% precision in the literature.
  Blocking automatically refuses about eight innocent transfers per real
  laundering leg.
- Telling someone they are under money-laundering review is **tipping off** - a
  criminal offence under India's PMLA, the US Bank Secrecy Act and the UK's
  Proceeds of Crime Act. A decline reading "flagged as suspicious" would commit
  that offence automatically, several times a day.

So the wire rail raises a case for a named human. The freeze itself is a
separate action in `risk/actions.py`, requires a reviewer, requires two above
one crore, and is recorded in an append-only log.

`CASH_OUT` is on the wire rail because it is where laundering *ends*. Value
pools in a mule account and leaves; a rail that watched the money arrive and not
depart lost sight of it at the moment the pattern completes.

Cash-in, fees, interest and settlement are not scored. None is a route an
attacker controls end to end.

## What the wire rail measures

Fitted on a clean run and evaluated on one with scripted laundering injected:

| | |
| --- | --- |
| false alarms on legitimate traffic | 0.53% |
| precision on the cases it raised | 87.4% |
| AUC | 0.967 |

Two things got it there from 28.6%, and the second mattered more.

**Signals are weighted by kind.** A structural inference - fan-out looks like a
payroll run, a cycle looks like a supply chain - carries about a third of the
bar, so a case needs two of them. A quantitative fact ("this account was
credited 32,000 rupees in six hours") is not an inference about intent, and a
rail demanding two of those would miss the single-primitive attacks it exists to
catch. The red-team playbook is explicit that findings "always carry a NUMBER".

**Value limits are fitted, not written.** A limit in rupees is a guess about an
economy. Measured, a hand-set 50,000-rupee owner limit sat *below* what an
ordinary account moves in a day and *above* what a mule chain moves in six legs
- it flagged the honest population and caught nobody, at 0% precision.
`calibrate()` fits the limits from traffic at a percentile derived from the
flag-rate budget, because a value signal raises a case alone, so the share of
traffic above a limit is roughly the share it flags.

## Why the IBM-trained model is not used

The `fraud_mastercard` repository has a wire model trained on IBM AML data
(`artifacts/wire_xgboost.json`, 49 features). All 49 are derivable here - the
four that look blocking (`payment_format`, `same_bank`, `currency`,
`currency_switch`) map to constants in a single-bank single-currency simulator.
So it was imported and measured.

| on the same 1,302 transfers | AUC |
| --- | --- |
| IBM-trained model | 0.723 |
| **hand-written rules** | **0.967** |

The reason is in the model's own gains: `payment_format` is its **strongest**
feature, `same_bank` second, `currency_switch` fourth, and the four carry
**31.6% of its total gain**. A tree splitting on a constant sends every row down
one branch, so importing it runs a model with its three best signals flatlined.

### Blending the scores makes it worse - but the model helps as a *signal*

Worth asking, because the two are genuinely different - their rank correlation
is 0.344, and of the 60 transfers in the model's top 5% that the rules did not
rank there, **all 60 were attackers**. The model is not uniformly worse; it is
worse on average and right about a specific set.

Measured anyway:

| | AUC |
| --- | --- |
| rules alone | **0.967** |
| mean of ranks | 0.918 |
| max of ranks | 0.924 |
| rules 0.8 / model 0.2 | 0.964 |

Every blend loses. Averaging makes one ranking, and a noisy member degrades it
everywhere - the disagreement set is small and the noise is not.

Splitting the review *budget* instead of the score could not be measured here:
the fixture runs 42% attacker-touching traffic, so any budget saturates at 100%
precision and the splits are indistinguishable.

**What does work is adding the model where every other piece of evidence
enters - as a `Signal` with a weight.** That makes the combination conjunctive
rather than averaged: a high model score raises a case when something structural
also fired, and alone it contributes without deciding.

| on the same 1,302 transfers | AUC | cases | caught | precision | recall |
| --- | --- | --- | --- | --- | --- |
| rules alone | 0.967 | 305 | 299 | 98.0% | 54.3% |
| **rules + model signal** | **0.972** | 353 | **345** | 97.7% | **62.6%** |

46 more attackers caught, precision holding at 97.7%, false alarms unchanged.
That is the same model that lost as a blend - what changed is how its opinion is
combined, not how good it is.

**Its threshold has to be fitted, and the default is deliberately unreachable.**
The four constant features compress its output: on simulator traffic it never
exceeds 0.27, so a hand-picked bar near 0.5 means the signal silently never
fires. A first attempt did exactly that and produced two arms with
byte-identical results - which reads as "the model adds nothing" and is really
"the model was never consulted". A probability from a model whose best splits
are inert is not comparable to one from the model as trained, so the bar comes
from the same clean traffic every other threshold is fitted on.

## Following the chain

A case saying "this transfer looks unusual" is not reviewable. The decision in
front of a reviewer is whether to freeze an account, and that needs the route:
which accounts, how much, how fast each leg closed, and where the money stopped.

`TransferGraph.trace_chain` follows the money forward hop by hop, and follows
only legs that **kept moving** - a hop is included when the receiving account
forwarded most of what arrived. An account that received money and held it is
where the chain ends; following past it would trace ordinary payments outward
forever.

Bounded on both axes: six hops, because a longer chain is not something a
reviewer reads, and four legs per hop, because a hub account would otherwise fan
the trace across the whole graph. When a hop had other legs the case says so, so
the reviewer knows one route was chosen rather than that only one existed.

Only cases that are actually raised get traced. Tracing every transfer would
walk the graph for the 98% of traffic nobody will look at. A trace that fails is
logged and dropped - it is context, not part of the decision, and a case is
still worth raising without it.

## Acting on a case

`POST /api/risk/cases/freeze`, `/clear`, `/step-up`. Every one requires a named
reviewer; `risk/actions.py` raises rather than defaulting to "system", because
"who decided this" is the first question an audit asks. A freeze above one crore
needs a second reviewer.

**A freeze is a request, not an act.** It records an intent and returns it. The
engine holds the funds and should act on an instruction with a case behind it,
not on a callback from a model.

**A step-up on the wire rail is refused, not ignored.** A challenge saying
"confirm this payment" tells the customer they are under review, and on an AML
case that is tipping off. The endpoint returns the reason rather than a generic
error, and the UI shows it.

Clearing does not delete. The rail flagged the case for a stated reason, and a
reviewer's judgement is one piece of evidence rather than the last word - the
log is append-only and a cleared case can be reopened.

## The card rail

A sequence model (TREASURE, arXiv:2511.19693) reading each account's recent
history. `models/card.pt` is committed so the branch can be cloned and run.

**No accuracy figure from the simulator should be quoted.** The fixture leaks:
`amount_over_account_mean` sits in [0.87, 1.13] for all 1,386 scripted frauds
while genuine accounts range far wider, and 30 attacker accounts against 776
genuine ones lets a model memorise signatures. The resulting AUC of 1.000
measures the generator.

What is verified is the pipeline: collect, train, save, load, score live,
threshold, flag. On a short run the model blocked 9 payments and challenged 2
out of 418.

### Thresholds move with the amount

Elkan's rule: the optimal boundary is `cost_fp / (cost_fp + cost_fn)`, and the
cost of missing fraud is the amount at risk. A large payment therefore faces a
lower bar.

The step-up and block ceilings are **separate**, at 0.75 and 0.97. Under a
single 0.99 ceiling a 25-rupee payment needed 0.952 confidence to be challenged
- and card testing uses those amounts deliberately, because a 25-rupee loss is
not worth stopping. The cost of missing it is not 25 rupees; it is the large
fraud the validated card then funds. A step-up is also not priced by the
transaction: it costs a genuine customer ten seconds whatever the amount.

## Retraining

The dashboard has a Retrain button. It does **not** make the model learn
continuously from live traffic:

- **A model that learns from its own decisions can be taught.** If "allowed"
  reads as "genuine", every fraud that gets through becomes a training example
  saying that shape is fine, so an attacker who finds one working attack widens
  it by repetition.
- **A blocked transaction never reveals whether it was fraud**, so the model
  would only learn from what it let through - biasing it toward whatever it
  already believed, invisibly.

Instead it trains a candidate, scores the candidate **and the live model** on a
period neither trained on, and promotes only if the candidate wins by more than
a point of recall. The live model is scored at that moment rather than compared
against its recorded number, which came from a different period against a
different vocabulary.

Rejected candidates are kept - they are evidence about what does not work.
Every promotion can be rolled back, because a holdout is a period and not the
future.

## What has not been measured

**The red team.** Every verdict about coverage is "a detector exists and would
fire on this shape", which is a reading of the code. Now that
`scripts/red_team_run.py` arms the rails by default, running it produces the
number that counts: what an adversary gets past a defended system.

**Long-chain layering.** The cycle search stops at six hops, so a sixteen-hop
chain is found only if a sub-cycle closes inside it. N-to-M routing that never
closes a cycle is invisible for the same reason. A pass-through propagation walk
would find both.

**The traffic generator.** Until it stops leaking labels, no card-model accuracy
figure means anything.
