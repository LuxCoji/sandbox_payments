# FinSim: Core Simulation Engine, Entities & Real-World Behavioral Logic

This document explains the foundational mechanics of the FinSim financial simulation engine. It describes the financial entities, the architectural blueprint from the original multi-contributor plan, how real-world banking rules are modeled, and how empirical data from millions of mobile money transactions was used to create a realistic, self-balancing artificial economy.

---

## 1. The Core Philosophy: Digital Twins of Banking Systems

At its heart, FinSim is built to answer a critical question: How can you safely test new banking rules, fraud detection models, and autonomous AI agents without risking real money or crashing a live bank?

To achieve this, FinSim acts as a financial digital twin. It does not simply generate synthetic transactions at random. Instead, it models the full lifecycle of modern retail payments, including user identities, hardware devices, merchant directories, bank clearing houses, KYC verification tiers, regulatory spending limits, and economic behavioral feedback loops.

---

## 2. The Architectural Blueprint: Contract-First, Event-Driven Design

When the initial plan for FinSim was drafted, three parallel contributors were assigned to build the platform simultaneously:
- Person 1 built the Core World Engine, Security Gateway, and the Discrete-Event Scheduler.
- Person 2 built the Population Model, Behavioral Intent Engine, and PaySim Calibration Pipeline.
- Person 3 built ChronoDAG, the Branch-Aware Event Store, State Hashing, and Observability.

To allow three engineers to build independent systems without blocking each other or introducing subtle bugs, the platform adopted two non-negotiable architectural foundations:

### Contract-First Boundaries
Before writing any business logic, all subsystem boundaries were locked in an interface contract document. Subsystems are forbidden from reaching into each other's internal code. A population agent or fraud model can only interact with the core engine through official public interfaces. This boundary is strictly enforced during continuous integration builds. If any developer attempts an unauthorized import between subsystems, the build fails immediately.

### Event Sourcing: History as the Single Source of Truth
Traditional software stores state by overwriting numbers in a database. If you have five hundred rupees and spend one hundred, the database simply overwrites five hundred with four hundred. If a system failure occurs, or if you need to know what your balance was on Tuesday at 3:15 PM, that historical context is lost.

FinSim operates on strict Event Sourcing:
1. An actor submits a Command (such as a request to transfer money).
2. The core engine checks the command against current balances and business rules.
3. If valid, the engine emits an immutable Domain Event (such as Account Debited or Payment Captured).
4. The event is permanently appended to the ChronoDAG event store.
5. In-memory balances and account snapshots are updated exclusively by applying that emitted event.

Because state is nothing more than the sum of all past events, the system can rewind time, replay any sequence of historical transactions, fork reality to test alternative scenarios, and produce identical, verifiable cryptographic hashes.

---

## 3. Financial Entities: How FinSim Mirrors the Real Banking World

Every entity in FinSim directly maps to real-world financial infrastructure, particularly retail payment networks such as UPI, IMPS, and debit card clearing houses.

### 3.1. Accounts
Accounts are the foundational containers of value. In FinSim, an account is not just a balance number; it carries real-world regulatory and structural metadata:

- Personal Accounts: Retail consumer accounts used for daily living expenses, peer-to-peer transfers, and merchant shopping.
- Merchant Accounts: Commercial business accounts that receive customer payments and settle batches with banks.
- Cash Entities: Physical touchpoints representing automated teller machines (ATMs) and bank branches where physical cash enters or leaves the digital network.
- Internal Settlement Accounts: Operational clearing accounts owned by the bank or central switch to hold funds during interbank clearing.
- Escrow Accounts: Neutral holding accounts for high-value transactions awaiting fulfilment before final settlement.

Every account also has a lifecycle status:
- Active: Normal operating status allowing debits and credits.
- Frozen: Administrative hold placed by compliance or fraud systems; all incoming and outgoing money movements are blocked.
- Closed: Permanently terminated account.
- Pending KYC: Restricted status for newly opened accounts awaiting identity verification.
- Disputed: Marked when an account is subject to an ongoing chargeback or fraud investigation.

### 3.2. KYC Levels and Regulatory Spending Limits
In real banking, regulatory bodies (such as the Reserve Bank of India) mandate Know Your Customer (KYC) tiers to prevent financial crime. FinSim models four distinct KYC levels:
- Level 0 (Minimum KYC): Unverified wallet or temporary account. Outgoing transactions are strictly capped at ten thousand rupees per day.
- Level 1 (Basic KYC): Self-verified identity with basic identification documents. Capped at twenty-five thousand rupees per day.
- Level 2 (Standard KYC): Standard bank account with verified tax and national identity documentation. Capped at one hundred thousand rupees per day.
- Level 3 (Full KYC / High Net Worth): Enhanced due diligence account with verified source of wealth. Capped at five hundred thousand rupees per day.

Special institutional accounts (Cash Entities, Settlement Accounts, and Escrow) carry an account multiplier of zero, meaning they have no artificial daily retail limit because they represent bank-level infrastructure.

### 3.3. Transaction Types: The 10 Financial Primitives
The engine natively supports ten distinct transaction typologies:
1. Payment: A customer purchasing goods from a merchant via a payment gateway.
2. Transfer: A peer-to-peer fund movement between two individual accounts.
3. Cash In: Depositing physical cash into a digital account at an ATM or branch.
4. Cash Out: Withdrawing digital balance as physical cash.
5. Debit: Incoming scheduled credits, such as monthly salary disbursements or automated vendor payouts.
6. Refund: A voluntary return of funds from a merchant back to a customer for returned goods.
7. Chargeback: A forced reversal initiated by a bank or credit rail following a dispute.
8. Settlement: End-of-day batch clearing moving accumulated funds from payment gateways to merchant operational accounts.
9. Fee: Transaction charges or platform service fees deducted by payment rails.
10. Interest: Periodic interest credited to consumer savings balances based on average daily balances.

### 3.4. Hardware Devices and Device Trust
In modern digital banking, payments originate from physical devices. FinSim explicitly models hardware endpoints:
- Device Types: Mobile phones, Point of Sale terminals, ATMs, and Web Browsers.
- Device Status: Active, Blocked, or Lost.
- Security Invariant: Transactions verify whether the originating device is registered and linked to the account owner, mirroring device-fingerprinting security in real banking apps.

### 3.5. Merchants and Directory Entries
Merchants are commercial entities assigned standardized four-digit Merchant Category Codes (MCCs) representing their industry (such as groceries, fuel, electronics, or hospitality). FinSim maintains a public merchant directory containing merchant identities, business categories, review ratings, and settlement rail preferences, enabling retail users to naturally discover and pay stores.

### 3.6. Fee Schedules and Rail Limits
Real payment systems charge complex fee structures and enforce cut-off windows:
- Flat fees versus percentage fees expressed in basis points (hundredths of a percent).
- Minimum and maximum fee boundaries to prevent unreasonable charges on micro-payments or macro-transfers.
- Daily settlement cut-off times, mirroring interbank batch settlement windows.

### 3.7. Actor Roles and Role-Based Privacy Masking
To replicate banking privacy laws (such as GDPR and financial data protection standards), actors do not have unrestricted access to the entire simulation database. When an actor queries the system, the engine generates an immutable, role-specific World View:
- Retail Users: See only their own accounts, registered devices, the public merchant directory, and global network fees.
- Merchants: See their own business accounts and pseudonymous customer transaction identifiers, but never customer account numbers or private balances.
- Bank Operations: See all system accounts, but sensitive Personally Identifiable Information (such as device IDs and customer names) is cryptographically masked.
- Risk Analysts: See aggregated flow graphs, velocity trends, and fraud model scores.
- Red Team Agents: Operating under a white-box security audit model, they can see account balances and KYC tiers across the branch to identify high-value targets, but are strictly prohibited from debiting accounts they do not own.

---

## 4. Grounding in Reality: PaySim Empirical Calibration

Rather than programming synthetic agents with arbitrary random behavior, FinSim's population is mathematically calibrated from PaySim—a landmark financial dataset derived from over twenty-four million real-world mobile money transactions.

### 4.1. Five Empirical Data Sources
The calibration pipeline processes five canonical statistical distributions:
1. Client Profiles: Empirical distributions of how frequently different customer demographics transact and their average spending power.
2. Aggregated Transactions: System-wide ratios of payments versus transfers versus cash withdrawals.
3. Initial Balances: Real-world wealth distributions, capturing the exact proportion of users who maintain low, medium, or high cash reserves.
4. Maximum Occurrences: Upper limits on how many times a single customer transacts within a single day.
5. Transaction Types: Baseline probabilities for retail purchases, ATM cash-outs, and peer-to-peer payments.

### 4.2. Lognormal Transaction Sizing
In human economies, transaction amounts are not distributed on a bell curve (normal distribution). Most transactions are small (buying a tea, paying for groceries), while a tiny fraction are very large (buying appliances, paying rent).

FinSim models financial amounts using lognormal distributions derived from arithmetic means and standard deviations. This ensures that the artificial population generates millions of authentic micro-transactions alongside realistic occasional high-value spikes, matching real-world retail payment traffic.

### 4.3. Diurnal Temporal Modeling (The 24x7 Rhythm of Life)
Human beings do not shop at 3:00 AM at the same rate they do at 2:00 PM on a Saturday.

FinSim incorporates a complete temporal rate matrix representing twenty-four hours across all seven days of the week:
- Activity naturally drops to near zero during nighttime hours (midnight to 5:00 AM).
- Morning spikes occur around breakfast and commute hours.
- Afternoon commercial peaks align with business and retail shopping.
- Weekend patterns exhibit higher peer-to-peer transfers and leisure spending compared to weekday commercial settlements.

User actions are scheduled using non-homogeneous Poisson processes, where the delay until an agent's next action is sampled dynamically based on the current day and hour in simulation time.

---

## 5. The Balance-Spring Behavioral Dynamic: Preventing Economic Collapse

A common failure in multi-agent financial simulations is economic death. In naive simulations, agents spend money randomly. Within a few thousand steps, most agents deplete their accounts to zero, fail their next transaction, and stop interacting. The economy grinds to a halt.

To solve this, FinSim implements the Balance-Spring Dynamic:

### The Mechanical Analogy
Imagine a physical spring attached to an account balance:
- When the balance is high, the spring is compressed. It exerts high outward force, boosting the probability that the agent will make discretionary purchases, transfer money to friends, or withdraw cash.
- When the balance drops low, the spring extends. Outward spending probabilities rapidly decay toward zero, while inward earning probabilities (depositing cash, receiving salary credits) increase.
- If an account balance hits zero, its probability of initiating an outgoing payment is strictly zero percent.

This dynamic creates a realistic, self-balancing artificial economy. Users naturally budget their funds, wait for scheduled income when low on balance, and spend surplus capital when solvent, sustaining stable financial activity over months of simulated time.

---

## 6. Determinism and Replay Invariance: Why Exact Numbers Matter

FinSim is built for repeatable science and rigorous auditability. If you discover a vulnerability or test a fraud detection algorithm, you must be able to reproduce the exact scenario down to the last paisa.

### 6.1. Currency as Integer Paise
Floating-point mathematics in computers introduces binary representation errors. If a million transactions calculate percentages using floating-point numbers, rounding errors accumulate, causing ledgers to drift and state hashes to fail. In FinSim, all currency is locked to 64-bit integer paise. One rupee is exactly one hundred paise. Every fee, balance deduction, and split calculation is exact and deterministic.

### 6.2. Independent Entity-Keyed Randomness
In traditional simulations, if an extra user is added to the simulation, the global random number generator is called an extra time, which shifts the random numbers received by every subsequent user, completely changing history.

FinSim solves this by using managed random streams. Every user, device, and merchant is assigned an independent random generator derived from its unique identity. Even if User A makes ten extra transactions on an experimental branch, User B's random decisions remain one hundred percent identical across both branches.

### 6.3. Genesis Provisioning vs. Transactional Commands
In early designs, creating initial user accounts during world setup was treated as a normal transaction. This caused confusion because opening a bank account is an administrative onboarding act, not a fund transfer between parties.

FinSim cleanly separates world bootstrapping from live operations. Initial population creation routes through a dedicated engine creation routine that establishes genesis accounts and emits initial creation events without cluttering transaction ledgers or invoking transactional rate limits.

---

## 7. Comparison: FinSim versus Real-World Financial Networks

FinSim was deliberately architected to mirror the exact functional components of modern national payment switches:

| Financial Component | Real-World Equivalent (e.g., UPI / Banking) | FinSim Digital Twin Implementation |
| :--- | :--- | :--- |
| **Monetary Unit** | INR / Paisa / Sub-units | Strict 64-bit Integer Paise (zero float drift) |
| **Identity Verification** | RBI Master Directions on KYC Tiers | 4-Tier KYC Model (Level 0 to Level 3) with Daily Volume Caps |
| **Retail Payment Flow** | Payer -> App -> Gateway -> Switch -> Merchant | Validated Command -> Domain Event -> ChronoDAG Log -> Aggregate Update |
| **P2P Transfer Flow** | Account-to-Account Immediate Settlement | Peer-to-Peer Transfer with Source Ownership Enforcement |
| **Cash In / Cash Out** | ATM Network / Cash Deposit Machines | Dedicated Cash Entity Aggregates with Physical Balancing |
| **Merchant Categorization** | ISO 18245 Merchant Category Codes | 4-Digit MCC System with Sector-Specific Behavioral Distributions |
| **Interbank Clearing** | End-of-day Multilateral Net Settlement | Internal Settlement Batches with Cut-Off Window Clocks |
| **Population Traffic** | Real National Mobile Payment Demographics | Calibrated from 24M+ PaySim Records with 24x7 Diurnal Poisson Rates |
| **Economic Stability** | Human Discretionary Budgeting & Income Cycles | Dynamic Balance-Spring Scaling preventing artificial insolvency |
| **Regulatory Privacy** | Bank Secrecy & Data Protection Standards | Role-Based Field Masking for Users, Merchants, and Operators |

---

*This document serves as the conceptual and behavioral reference for FinSim's core engine and entity architecture.*
