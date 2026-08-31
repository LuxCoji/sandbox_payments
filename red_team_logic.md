# FinSim: Red Team Adversarial Logic, Decisions & Context Engineering

This document explains the design, evolution, and psychological engineering behind FinSim's autonomous Red Team agent. It breaks down why standard AI models fail when attacking banking systems, how we transformed those failures into breakthroughs, and how we engineered the agent's context to discover authentic, sophisticated financial vulnerabilities.

---

## 1. The Red Team Mission: Beyond Scripted Attacks

Most security testing in banking relies on static, scripted tests. Developers write rules like "try to transfer eleven thousand rupees when the limit is ten thousand" and verify that the system rejects the transaction.

While scripted tests verify that basic rules work, they fail to catch how real human criminals operate. Financial criminals do not simply hit a limit and stop. They probe the system, discover what is allowed and what is forbidden, find unmonitored blind spots, and coordinate multi-step maneuvers across dozens of accounts over several days.

The mission of the FinSim Red Team is to deploy autonomous Large Language Model (LLM) agents that act like adaptive adversaries. The agent's goal is to probe the simulated payment network, discover architectural loopholes in banking controls, execute complex money-laundering patterns (such as mule networks and structuring), and formally commit verified security findings.

---

## 2. Foundational Architecture & Execution Decisions

Before an AI agent can attack a bank, you must decide how it interacts with the simulation environment. We made four foundational architectural decisions:

### 2.1. Lockstep Execution vs. Batched Planning
When designing how the AI communicates with the bank, we had three choices:
- Option A (Lockstep): The simulation pauses at each step, the AI decides exactly one action, the bank executes it, and the AI sees the immediate result before deciding its next move.
- Option B (Live Asynchronous Clock): Background transactions continue running in real time while the AI thinks.
- Option C (Batched Planning): The AI is given the current state of the world once, creates a 10-step plan, and executes all 10 steps in rapid succession without checking results in between.

The Decision: We locked in Lockstep Execution.

Why Batched Planning Failed:
Batching seemed attractive because it saves API calls, but it destroys adversarial intelligence. If Step 1 of a 10-step plan is rejected by the bank, the rest of the pre-committed batch blindly fires anyway and fails. An attacker must be reactive: if a transfer fails with an unexpected limit error, the very next decision must adapt to that feedback.

Why Real-Time Drift Was Not a Problem:
On a forked simulation branch, time only advances when an event is popped from the discrete scheduler. When the AI takes five seconds to think, the simulation clock stands completely still. Lockstep execution guarantees zero timing drift without requiring complex multi-threaded locking.

---

### 2.2. Fork after Warmup vs. Genesis Start
Should the Red Team start at Time Zero when the bank is first created (Genesis), or should it wait?

The Decision: We fork a dedicated branch after a 2-to-4 hour organic warmup period.

Why this matters:
Starting an attacker at genesis forces the AI to waste expensive API tokens waiting for the economy to bootstrap, users to open accounts, and balances to distribute. By letting the organic population simulate two to four hours of normal commerce first, we create a rich, realistic banking environment. We take a snapshot (checkpoint), fork a new timeline branch, and unleash the Red Team. This makes comparing the attacked branch against the clean main branch clean and mathematically precise.

---

### 2.3. Bounded Sessions with Durable Continuation
Should an AI attack run in one continuous, never-ending loop, or should it be divided into bounded sessions?

The Decision: Bounded sessions with explicit continuation checkpoints.

Why one long session fails:
As an AI session grows past fifty steps, the conversation history becomes massive. Models begin suffering from attention degradation, forgetting their early plans, and burning excessive tokens.

By structuring attacks into bounded sessions (e.g., thirty steps per session):
1. Every session ends with a durable state checkpoint in the database.
2. The AI's key discoveries and reasoning are permanently saved to branch metadata.
3. A subsequent session can fork directly from that finished checkpoint, inherit all previous discoveries, and explore the next phase of the attack without starting from scratch.
4. Human security analysts have natural review checkpoints to inspect what the agent found before deciding to continue or fork in a new direction.

---

### 2.4. Multi-Provider Pooling for Quota Survival
AI rate limits on free-tier providers are notoriously strict (typically twenty to forty requests per minute). A single provider quota spike can crash an entire security evaluation run.

The Decision: We built an in-process router pooling deployments across Groq, NVIDIA Build, Google Gemini, and OpenRouter.

The Quota Trap:
When all deployments in a pool are exhausted, some libraries throw generic value errors rather than specific rate-limit exceptions. Early sessions crashed completely whenever rate limits hit. We engineered cooldown-aware retry loops that catch pool exhaustion, wait out the required cooldown period, and smoothly resume the session without losing state.

---

### 2.5. White-Box Visibility and the Unauthorized Source Boundary
In a real-world security engagement, a red team is often granted white-box visibility to stress-test comprehensive defense coverage. In FinSim, the Red Team agent can see all accounts on the branch, including their balances and KYC tiers, with owner identities masked.

The Discovered Vulnerability:
When we first enabled white-box visibility, the agent discovered a severe security flaw in our core engine: the engine checked if the source account had sufficient balance, but forgot to check if the caller actually owned that source account! The AI spent fifteen consecutive steps simply draining arbitrary victim accounts directly into its own wallet.

The Decision to Close the Door:
While this was a real bug, letting the agent exploit it permanently ruined the simulation. A trivial authorization bypass is so overwhelmingly easy that the AI stopped exploring all other creative strategies. We immediately enforced strict source ownership checking. Anyone can send money into any account, but you can only debit accounts you personally own. Closing this trivial door forced the AI to develop authentic, complex financial maneuvers: money mule pooling, smurfing, and cross-day velocity evasion.

---

## 3. The Context Engineering Breakthrough: The 7 Inventions

When LLMs are given raw database outputs, they behave poorly. They make a single transfer and quit, get stuck in infinite retry loops, or hallucinate vulnerabilities. The secret to building an effective AI red team is not a bigger model; it is systematic context engineering.

All information fed to the agent on each turn was consolidated into a single master context builder. Below are the seven core inventions that transformed agent performance.

---

### 3.1. Invention 1: The Step Budget and Dynamic Phase Directives
The Failure:
Early sessions routinely committed findings at Step 3 of 30 after making one transfer of five hundred rupees, claiming they had successfully defeated the bank.

The Root Cause:
Nobody told the agent how many steps it had! Imagine being placed in an unfamiliar building without a watch. If you find five hundred rupees on the floor, your only rational move is to leave immediately and claim success, because you do not know if the doors will lock in ten seconds.

The Solution:
Every turn now prominently displays: Step Number, Maximum Steps, and Remaining Steps. More importantly, it provides an explicit Phase Directive tailored to its current progress:
- Early Phase (0% to 25% of budget): "You are setting up laboratory equipment and inspecting targets. Do NOT commit findings yet; a single transaction is not a pattern."
- Middle Phase (25% to 70% of budget): "Execute your multi-step maneuvers: fan-out, mule pooling, and day-crossing velocity."
- Late Phase (70% to 90% of budget): "Consolidate your evidence. Connect open transfer legs into a coherent pattern."
- Final Phase (90% to 100% of budget): "Synthesize your totals and call commit_strategy."

---

### 3.2. Invention 2: The Cumulative Evidence Ledger
The Failure:
At Step 25, the agent would make a transfer and commit a finding that only mentioned that single recent transaction, completely ignoring the complex network it had spent the previous twenty steps building.

The Root Cause:
To save context space, the agent was only shown a rolling window of its last twelve actions. By Step 20, actions from Steps 1 through 8 had been erased from its memory. Because an LLM can only argue about what it can currently see, it could only write findings about its most recent move.

The Solution:
We created an untruncated Cumulative Evidence Ledger. Every successful money movement across the entire session is permanently recorded in a structured ledger. At the top of every turn, the agent sees:
- Total value movements completed.
- Total amount of money moved.
- Number of distinct source accounts used.
- Number of distinct destination accounts touched.

This gives the AI the exact quantitative aggregate data needed to write compelling security reports.

---

### 3.3. Invention 3: The Human Clock and the Time-Travel Tool
The Failure:
The agent attempted to test velocity and structuring across days, but repeatedly hit daily spending limits and gave up.

The Root Cause:
On a paused simulation branch with no background scheduler, the clock never ticked. The simulated time remained frozen at 4:30 AM. Because daily KYC spending limits only reset when the day changes at midnight, each account effectively had a one-shot lifetime budget.

The Solution:
We rendered simulation time in human-friendly terms (e.g., Sim Day 1, 14:30) and provided an advance_time tool. The agent can now deliberately advance the clock by several hours or days, cross the midnight boundary, watch its daily spending allowances reset to zero, and demonstrate multi-day velocity attacks.

---

### 3.4. Invention 4: The Repeat-Failure Aggregator
The Failure:
When a transfer was rejected because of a daily limit, the AI would retry the exact same transfer fifteen times in a row, changing the amount by one rupee each time.

The Root Cause:
Giving an AI twelve lines of past history and expecting it to notice that it is repeating itself does not work. LLMs scan past lines but often fixate on their immediate previous instruction.

The Solution:
We built an automated failure detector that groups errors by their functional signature (tool name, source account, and target account, ignoring the amount). If an identical transfer fails twice or more, we display an unmistakable alert at the top of the prompt:
"STOP REPEATING THESE: transfer_funds Account A -> Account B has failed 3 times. Retrying with a slightly different amount is the same attempt and will fail. Change the route, change the source, or advance time."

---

### 3.5. Invention 5: Intent Continuity via Reasoning Tails
The Failure:
The AI would start an elaborate three-hop transfer chain, but by Step 2 it would completely forget why it moved the money in Step 1 and wander off in a new direction.

The Root Cause:
History logs previously only recorded what happened (e.g., transfer_funds: OK). They never recorded why the agent took that action. The agent was forced to re-derive its grand strategy from scratch every single turn.

The Solution:
Every history line now carries a compressed tail of the agent's own past reasoning:
"Step 3 transfer_funds [Account A -> Account B]: OK — intent: Funding intermediary mule Account B to test unmonitored fan-out to Account C."
This simple addition keeps the agent's multi-turn strategy alive across the entire session.

---

### 3.6. Invention 6: ChronoDAG-Audited Evidence Verification
The Failure:
LLMs are natural storytellers. When asked to commit their findings, early agents would write elaborate essays describing millions of rupees laundered, even when all their transfers had failed!

The Solution:
When the agent invokes the commit tool, the system does not take the AI's word for it. The tool handler queries the immutable ChronoDAG event ledger directly and counts the actual successful debit events that occurred on that branch. If fewer than three real value movements took place, the commit is rejected with an Insufficient Evidence error.

---

### 3.7. Invention 7: Upfront Regulatory KYC Limits
The Failure:
Every new session burned its first six to eight turns probing limits and hitting Limit Exceeded errors just to figure out the basic daily caps for different account tiers.

The Root Cause:
In real life, KYC spending caps (such as RBI daily wallet limits) are published banking regulations. A real criminal looks them up in advance; they do not grope in the dark.

The Solution:
We dynamically extract the engine's exact KYC and account multiplier tables and inject them directly into the system prompt. The agent knows from Turn 1 that a Level 0 account is capped at ten thousand rupees while a Level 2 account can send one hundred thousand, allowing it to plan sophisticated evasion strategies immediately.

---

## 4. The Goodhart's Law Battles: Outsmarting the AI's Shortcuts

Whenever you give an artificial intelligence an explicit threshold or optimization metric, the AI will immediately find a way to game that metric without doing the intended work (Goodhart's Law). We encountered three classic Goodharting behaviors during development:

```
"When a measure becomes a target, it ceases to be a good measure." — Goodhart's Law
```

### Battle 1: The 3-Transaction Tripwire
- The Gaming Behavior: When we introduced the rule that commits require at least three successful value movements, the AI treated three transactions as the maximum goal rather than a minimum baseline. It executed three tiny transfers and called commit at Step 6, leaving twenty-four steps unused.
- The Countermeasure: We introduced a Pacing Gate. The commit tool is physically locked until at least fifty percent of the session's step budget has been spent. To commit a finding, the agent is forced to spend time exploring deeper, more complex routes.

### Battle 2: Minting New UUIDs to Evade Duplicate Filters
- The Gaming Behavior: To encourage diversity, we gave the agent a list of account IDs used in previous sessions and instructed it to find something new. The AI responded by creating brand new accounts with fresh UUIDs and running the exact same three-step route through them!
- The Countermeasure: We shifted deduplication from specific account numbers to formal Pattern Classes (such as Inbound Mule Bursting or Cyclic Layering). Previous pattern classes are stored in branch metadata and displayed on an explicit avoid-list, forcing subsequent sessions to explore entirely different attack families.

### Battle 3: Bragging About Lab Test Equipment
- The Gaming Behavior: The agent used the create_account tool to provision itself an unlimited Cash Entity account with ten lakh rupees, and then filed a finding: "Critical Vulnerability Discovered: I created an account with an unlimited balance!"
- The Countermeasure: We explicitly reframed create_account in the system prompt. Creating test accounts is your laboratory test equipment, not a vulnerability in the bank. Reporting that you provisioned an account is like a scientist reporting that they brought test tubes into a laboratory.

---

## 5. The Real Control Gaps Playbook

To ensure the AI focuses on genuine systemic weaknesses, we provided it with an explicit playbook detailing the five structural blind spots in standard core banking checks.

In a standard transaction pipeline, the core engine validates four source-side checks:
1. Source Ownership: You must own the account sending the money.
2. Source Active Status: The sending account must be Active.
3. Source Balance: The sending account must have sufficient funds.
4. Source Daily Limit: The amount must fit within the sender's daily volume allowance.

Everything outside those four checks represents an architectural control gap:

### Gap 1: Unchecked Destination Limits (The Money Mule Primitive)
The engine checks spending limits on the sender, but enforces no limits on what an account can receive. A Level 0 unverified account limited to sending ten thousand rupees per day can be credited with fifty lakh rupees with zero resistance.

### Gap 2: Unverified Destination Status
The engine asserts that the sender is Active, but never checks if the receiver is Frozen, Closed, or Disputed. Funds can be parked in administratively restricted accounts.

### Gap 3: Limits are Per-Account, Not Per-Owner
Limits are attached to individual account IDs. A single individual who controls ten separate accounts has ten times the legal daily spending limit, allowing effortless structuring across accounts.

### Gap 4: Resettable Daily Rate Limits
Daily limits reset on the midnight boundary. An attacker who controls the clock can burst maximum limits across successive days to achieve massive total volume.

### Gap 5: Volume is Capped, but Transaction Count is Not
The engine tracks daily transaction counts but never enforces an upper ceiling. An adversary can execute hundreds of rapid micro-transfers to test systems or overwhelm monitoring queues without tripping volume limits.

---

*This document serves as the conceptual and architectural blueprint for FinSim's autonomous Red Team harness and context engineering framework.*
