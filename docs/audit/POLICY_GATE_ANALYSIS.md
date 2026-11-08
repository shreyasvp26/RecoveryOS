# RecoveryOS — Policy Detection & Policy Gate Deep-Dive Analysis

> **Priority Level**: Highest Priority (Phase 2 & Phase 3 Core Audit)
> **Primary Source Files**: `backend/app/policy.py:1-497`, `backend/app/db.py:638-673`, `backend/app/config.py:84-126`, `backend/app/execution_service.py:185-490`, `backend/app/executor.py:166-267`, `backend/app/optimizer.py:124-275`, `backend/app/policy_scenario.py:1-610`, `backend/app/replay.py:265-430`, `backend/app/replay_metrics.py:216-235,546-578`.

## 1. Executive Verdict on the Policy Gate

The policy gate (`PolicyEngine.evaluate()` in `backend/app/policy.py:345-417`) is cleanly structured as a pure, side-effect-free function over `PolicyInput` and `PolicyConfig`, and its downstream authorization wrapper (`AllowedCandidates` in `backend/app/optimizer.py:124-268` + `BoundedExecutor._verify_authorization()` in `backend/app/executor.py:132-164`) genuinely prevents a policy-denied candidate from being selected by the optimizer or executed by `BoundedExecutor` (**VERIFIED**).

**However, the policy gate has 7 severe design, detection, and state-tracking flaws that break its real-world and operational guarantees:**

1. **LLM Fraud Diagnosis Is Completely Ignored by `RULE_FRAUD` (`policy.py:362-363`, `failed_payment_ingestion.py:130`) — CRITICAL**:
   - `PolicyEngine.evaluate()` checks `if event.risk_flag == "fraud_suspect": return _denied(RULE_FRAUD)` (`policy.py:362`), and checks `if classification.root_cause_category == "terminal": return _denied(RULE_TERMINAL)` (`policy.py:365`), **but NEVER checks `if classification.root_cause_category == "fraud_suspect":`**!
   - Meanwhile, every real Razorpay `payment.failed` webhook ingested via `map_failed_payment_to_event()` (`backend/app/failed_payment_ingestion.py:130`) hardcodes `risk_flag="normal"`.
   - Consequently, if the LLM diagnoses a live webhook event as `root_cause_category="fraud_suspect"` and proposes any executable candidate (or if `classify_event` returns `root_cause_category="fraud_suspect"` with `["payment_link"]`), **the policy gate passes `"fraud_check_passed"` and authorizes the intervention!**
   - Worse still, `replay_metrics.py:224` (`fraud_interventions`) checks `record.root_cause_category == "fraud_suspect"` to count unauthorized fraud interventions in Policy Lab, so an event with `risk_flag="normal"` and `root_cause_category="fraud_suspect"` causes `verify_comparison_fairness()` (`replay_metrics.py:567-570`) to fail `immutable_protections_held` and crash `compare_replays()` with a `ValueError`!

2. **Failed Execution Attempts (Including `configuration_missing`) Poison Policy History (`execution_service.py:447-455,475`, `db.py:638-673`) — HIGH**:
   - When `BoundedExecutor.execute()` returns `status="FAILED"` (for example, `payment_link` fails because `razorpay_client is None` -> `configuration_missing`, or Razorpay returns HTTP 400 `BadRequestError`), `execution_service.py:447-455` inserts an `InterventionAttempt` row with `status="failed"` and `cost_paise=config.intervention_cost(selected)` (`100` paise for `payment_link`).
   - Then `execution_service.py:475` releases the `execution_claims` row so the event can be retried after fixing config.
   - **Bug**: `db.get_policy_history()` (`db.py:638-673`) counts **ALL** rows in `intervention_attempts` regardless of `status` (`attempted`, `failed`, `successful`) for:
     - `most_recent_event_intervention_time` (`db.py:652-658`) -> **blocks the event for 30 minutes (`RULE_COOLDOWN`)** even though no intervention actually ran!
     - `customer_intervention_count_24h` (`db.py:644-650`) -> **after 2 failed config attempts, blocks the customer for 24 hours (`RULE_CUSTOMER_LIMIT`)**!
     - `existing_daily_spend_paise` (`db.py:674-682`) -> **charges 100 paise against the daily spend cap** for a Payment Link that was never created!

3. **`RULE_DUPLICATE` Treats Link Creation (`PENDING_OUTCOME`) as Final Success, Blocking All Follow-Up Reminders (`execution_service.py:453`, `policy.py:368-369`, `db.py:660-672`) — HIGH**:
   - When `BoundedExecutor` creates a Razorpay Payment Link (or simulates a reminder), `execution_service.py:444-455` immediately records `InterventionAttempt(status="successful")` in `intervention_attempts`.
   - `db.get_policy_history()` sets `has_successful_intervention = True` whenever ANY `intervention_attempts` row for `event_id` has `status = 'successful'` (`db.py:660-672`).
   - Therefore, as soon as a Payment Link is created (`PENDING_OUTCOME` — customer has NOT paid), `RULE_DUPLICATE` permanently blocks any follow-up `reminder` or `alternate_method_prompt` on that `event_id`, even if the customer ignores the link or the link expires!

4. **Per-Event Rules (`RULE_DUPLICATE` & `RULE_COOLDOWN`) Are Keyed by `event_id` Instead of `order_id` (`db.py:652-672`, `failed_payment_ingestion.py:92-104`) — HIGH**:
   - In Razorpay, each failed payment attempt on the same order generates a new `payment_id` and a new webhook `delivery_id`, which `failed_payment_ingestion.py:92-104` maps to a **new `event_id`** (`evt_rfp_<hash>`).
   - Because `db.get_policy_history()` queries `most_recent_event_intervention_time` and `has_successful_intervention` strictly by `WHERE event_id = ?` (`db.py:655,663`) rather than `order_id`, a customer who fails twice on the same `order_id` within 1 minute bypasses both `RULE_COOLDOWN` (30-min event cooldown) and `RULE_DUPLICATE` (can be sent two active Payment Links for the exact same order)!

5. **`POST /events/{event_id}/policy` Allows Arbitrary Caller-Supplied `proposed_intervention` Not in LLM Candidates & Fails with HTTP 500 on Same-Timestamp Re-evaluation (`routes/events.py:293-387`) — MEDIUM**:
   - `PolicyEngine.evaluate()` (`policy.py:345-417`) never checks whether `proposed_intervention in classification.candidate_interventions`! While `execution_service.py:194` only loops over `classification.candidate_interventions`, `POST /events/{event_id}/policy` accepts any valid intervention string in the request body (`routes/events.py:312-320`) and persists an `allowed=True` decision in `policy_decisions` even if the LLM only proposed `["no_action"]`.
   - Additionally, if `POST /events/{event_id}/policy` is called twice with the same `(event_id, proposed_intervention, evaluation_time)`, `insert_policy_decision` raises `sqlite3.IntegrityError`, which `routes/events.py:368-378` returns as **HTTP 500** (`policy_decision_persistence_failure`) instead of idempotently returning the existing decision (unlike `execution_service.py:216-231` which catches `IntegrityError` and re-reads via `get_policy_decision`).

6. **Phase 17 Benchmark `frozen_policy_config()` Uses Zero Costs for All Interventions (`benchmark_config.py:55-68`, `policy.py:114-116`) — HIGH**:
   - Phase 26 (`config.py:113-125`) wired `default_intervention_cost_paise()` (`payment_link: 100`, `alternate_method_prompt: 50`, `reminder: 25`) into `build_policy_config()` and `policy_scenario.py`, **but forgot to update `benchmark_config.frozen_policy_config()`** (`benchmark_config.py:55-68`), which constructs `PolicyConfig(...)` without `intervention_cost_paise`.
   - Because `PolicyConfig.__post_init__` defaults `intervention_cost_paise` to `{}` (`policy.py:114-116`), every intervention in Phase 17 benchmark has `config.intervention_cost(i) == 0`, making `RULE_SPEND_CAP` a complete no-op across the entire Phase 17 benchmark!

7. **Missing Real-World Payment Compliance Constraints — HIGH**:
   - **Card Network Retry Rules (Visa/Mastercard)**: No distinction between hard-decline codes (e.g., stolen card, invalid account, closed account -> mandatory 0 retries) vs soft declines, and no cap on merchant-initiated card retries over 30 days (Visa/Mastercard cap merchant-initiated retries at 15 attempts over 30 rolling days).
   - **RBI e-Mandate / Standing Instruction Rules (India)**: For `upi` and `card` subscriptions (`has_active_subscription=True`), RBI mandates a 24-hour pre-debit notification before executing a recurring debit and restricts retry presentation on insufficient funds. `PolicyEngine` has zero awareness of `payment_method`, `has_active_subscription`, or `amount_paise` (e.g., ₹15,000 AFA threshold for Indian recurring mandates).
   - **Customer Consent / DND / Quiet Hours**: `reminder`, `payment_link`, and `alternate_method_prompt` send outbound customer communications without checking customer opt-out/consent or time-of-day quiet hours (TRAI DND regulations in India prohibit commercial messages between 9 PM and 9 AM IST).

## 2. Complete Specification of All 6 Policy Rules (`backend/app/policy.py`)

### 2.1 Inputs & Configuration Schema

- **`PolicyConfig` (`policy.py:103-155`)** — **VERIFIED**:
  - `max_interventions_per_customer_24h: int = 2` (must be `>= 1`)
  - `event_cooldown_minutes: int = 30` (must be `>= 1`)
  - `daily_spend_cap_paise: int = 5_000_000` (must be `>= 0`)
  - `intervention_cost_paise: Mapping[str, int]` (defaults to `{}` in `PolicyConfig()`, meaning `0` paise for all interventions unless passed by `config.build_policy_config()`, which injects `DEFAULT_ECONOMIC_MODEL` costs: `retry_immediate: 0`, `retry_delayed: 0`, `reminder: 25`, `alternate_method_prompt: 50`, `payment_link: 100`).
- **`PolicyHistory` (`policy.py:158-203`)** — **VERIFIED**:
  - `customer_intervention_count_24h: int` (count of rows in `intervention_attempts` for `customer_id` with `attempted_at` in `[evaluation_time - 24h, evaluation_time]`, `db.py:644-650`).
  - `most_recent_event_intervention_time: datetime | None` (max `attempted_at` in `intervention_attempts` for `event_id` with `attempted_at <= evaluation_time`, `db.py:652-658`).
  - `has_successful_intervention: bool` (whether any row in `intervention_attempts` for `event_id` has `status == 'successful'` and `attempted_at <= evaluation_time`, `db.py:660-672`).
  - `existing_daily_spend_paise: int` (sum of `cost_paise` across ALL customers/events in `intervention_attempts` with `attempted_at` in `[evaluation_time - 24h, evaluation_time]`, `db.py:674-682`).
  - *Note on SQLite Timestamp Filtering (`db.py:617-635`)*: `db._attempts_up_to()` loads ALL rows from `intervention_attempts` (or all rows for `customer_id` / `event_id`) into Python memory and parses each `attempted_at` with `parse_aware_datetime()` (`db.py:625-635`). This avoids SQLite string-comparison bugs across timezones, but causes an $O(N)$ full-table scan of `intervention_attempts` on every single policy evaluation!

### 2.2 Exact Rule Definitions, Precedence & Short-Circuit Behavior

`PolicyEngine.evaluate(policy_input, config)` (`policy.py:345-417`) validates `policy_input` and `config`, rejects `proposed_intervention == "no_action"` or unknown interventions (`policy.py:239-243` — fail-closed via `PolicyValidationError`), and then evaluates the 6 rules sequentially in `DETERMINISTIC_RULE_ORDER` (`policy.py:30-37`).

**Short-Circuit Conflict Handling**: The first rule that triggers a denial immediately returns `PolicyDecision(allowed=False, denial_reason=<rule_name>, policy_rules_applied=(*passed_so_far, <rule_name>))` (`policy.py:397-417`). Subsequent rules are not evaluated. Only if all 6 rules pass does `evaluate()` return `allowed=True` with `policy_rules_applied = ("fraud_check_passed", "terminal_check_passed", "duplicate_check_passed", "retry_limit_passed", "cooldown_check_passed", "spend_cap_passed")`.

| Order | Rule Constant (`denial_reason`) | Pass Marker | Exact Code Condition (`policy.py:362-392`) | Mutable in Policy Lab? |
|---:|---|---|---|---|
| 1 | `fraud_protection` | `fraud_check_passed` | `event.risk_flag == "fraud_suspect"` (`policy.py:362`) | No (Immutable) |
| 2 | `terminal_failure` | `terminal_check_passed` | `classification.root_cause_category == "terminal"` (`policy.py:365`) | No (Immutable) |
| 3 | `duplicate_intervention` | `duplicate_check_passed` | `history.has_successful_intervention is True` (`policy.py:368`) | No (Immutable) |
| 4 | `customer_intervention_limit_exceeded` | `retry_limit_passed` | `history.customer_intervention_count_24h >= config.max_interventions_per_customer_24h` (`policy.py:371-374`) | Yes (`1..10`) |
| 5 | `event_cooldown_active` | `cooldown_check_passed` | `recent is not None and (evaluation_time - recent) < timedelta(minutes=config.event_cooldown_minutes)` (`policy.py:377-384`) | Yes (`1..1440` min) |
| 6 | `spend_cap_exceeded` | `spend_cap_passed` | `history.existing_daily_spend_paise + config.intervention_cost(proposed) > config.daily_spend_cap_paise` (`policy.py:387-391`) | Yes (`0..500,000,000` paise) |

### 2.3 Can the Policy Gate Be Bypassed?

- **At the Optimizer Boundary (`backend/app/optimizer.py:124-268`)**: **No** (**VERIFIED**). `AllowedCandidates.__post_init__` (`optimizer.py:141-190`) re-verifies that every intervention in `allowed` is present in `considered`, is an executable intervention (not `no_action`), and has a `PolicyDecision` in `decisions` with `decision.allowed is True` and `decision.proposed_intervention == candidate`. `EconomicInterventionOptimizer.select()` (`optimizer.py:310-326`) additionally verifies `decision.event_id == event.event_id`.
- **At the Executor Boundary (`backend/app/executor.py:132-164`)**: **No** (**VERIFIED**). `BoundedExecutor._verify_authorization()` checks `isinstance(decision, PolicyDecision)`, `decision.allowed is True`, `decision.event_id == event.event_id`, and `decision.proposed_intervention == intervention`.
- **At the HTTP Route Boundary (`backend/app/routes/recovery.py:194-224`, `routes/events.py:390-485`)**: **No** (**VERIFIED**). `POST /recovery/{event_id}/execute` rejects any non-empty JSON body containing keys with HTTP 422 `client_authority_rejected`, and `POST /events/{event_id}/execute` ignores request bodies and always recomputes policy decisions from SQLite state via `execute_event()`.
- **Logical Bypass via Incomplete Rule Definition (`policy.py:362`, `db.py:655,663`)**: **YES** (**VERIFIED**):
  1. An LLM diagnosis of `root_cause_category == "fraud_suspect"` on an event with `risk_flag == "normal"` (which is true of 100% of Razorpay webhook events in `failed_payment_ingestion.py:130`) **bypasses `RULE_FRAUD` completely**.
  2. A second `payment.failed` webhook on the **same `order_id`** creates a new `event_id` and **bypasses both `RULE_DUPLICATE` and `RULE_COOLDOWN`**.

## 3. Step-by-Step Traced Examples Through the Policy Gate (6 Concrete Traces)

In all traces below, let `evaluation_time = 2026-08-27T13:00:00+00:00` and let `config = build_policy_config()` (`max_interventions_per_customer_24h=2`, `event_cooldown_minutes=30`, `daily_spend_cap_paise=5_000_000`, `intervention_cost_paise={"retry_immediate": 0, "retry_delayed": 0, "reminder": 25, "alternate_method_prompt": 50, "payment_link": 100}`).

### Trace 1: Normal Transient Failure (`bank_timeout`) — Allowed & Economically Ranked
- **Inputs**:
  - `event`: `event_id="evt_trace_1"`, `customer_id="cust_01"`, `amount_paise=100_000` (₹1,000), `payment_method="card"`, `failure_reason="bank_timeout"`, `risk_flag="normal"`, `customer_history=(4, 1, True)`.
  - `classification`: `root_cause_category="transient"`, `candidate_interventions=("retry_delayed", "payment_link")`.
  - `history`: `customer_intervention_count_24h=0`, `most_recent_event_intervention_time=None`, `has_successful_intervention=False`, `existing_daily_spend_paise=0`.
- **Step-by-Step Evaluation (`PolicyEngine.evaluate`, `policy.py:356-403`)**:
  - For `proposed_intervention = "retry_delayed"` (and identically for `"payment_link"`):
    1. `event.risk_flag == "fraud_suspect"` -> `False` -> appends `"fraud_check_passed"` (`policy.py:362-364`).
    2. `classification.root_cause_category == "terminal"` -> `False` -> appends `"terminal_check_passed"` (`policy.py:365-367`).
    3. `history.has_successful_intervention` -> `False` -> appends `"duplicate_check_passed"` (`policy.py:368-370`).
    4. `0 >= 2` -> `False` -> appends `"retry_limit_passed"` (`policy.py:371-376`).
    5. `recent is None` -> skips cooldown check -> appends `"cooldown_check_passed"` (`policy.py:377-385`).
    6. Spend check (`policy.py:387-392`):
       - For `"retry_delayed"`: `0 + 0 <= 5_000_000` -> appends `"spend_cap_passed"`.
       - For `"payment_link"`: `0 + 100 <= 5_000_000` -> appends `"spend_cap_passed"`.
- **Gate Output**: Both `"retry_delayed"` and `"payment_link"` return `PolicyDecision(allowed=True, denial_reason=None, policy_rules_applied=(all 6 pass markers))`.
- **Downstream Selection (`optimizer.py:328-363`)**:
  - `RecoveryProbabilityEstimator` (`estimator.py:173-233`) scores `"retry_delayed"` at `5200 + 800 + 700 + 100 + 300 + 350 = 7450 bps` (`EV = 74,500 - 0 - 50 = 74,450 paise`) and `"payment_link"` at `4800 - 200 + 100 + 0 - 200 + 350 = 4850 bps` (`EV = 48,500 - 100 - 100 = 48,300 paise`).
  - Optimizer selects `"retry_delayed"`.

### Trace 2: Multi-Violation Conflict (`fraud_suspect` + `terminal` + Customer Limit Exceeded) — Precedence Short-Circuit
- **Inputs**:
  - `event`: `event_id="evt_trace_2"`, `customer_id="cust_02"`, `risk_flag="fraud_suspect"`.
  - `classification`: `root_cause_category="terminal"`, `candidate_interventions=("retry_delayed",)`.
  - `history`: `customer_intervention_count_24h=5` (`>= 2`), `has_successful_intervention=True`, `existing_daily_spend_paise=9_000_000` (`> 5_000_000`).
- **Step-by-Step Evaluation (`policy.py:362-363`)**:
  1. Rule 1 (`fraud_protection`): `event.risk_flag == "fraud_suspect"` is `True`!
  2. Immediately calls `_denied(policy_input, applied, RULE_FRAUD)` (`policy.py:363`).
  3. Rules 2 (`terminal_failure`), 3 (`duplicate_intervention`), 4 (`customer_intervention_limit_exceeded`), 5 (`event_cooldown_active`), and 6 (`spend_cap_exceeded`) are **never evaluated**.
- **Gate Output**: `PolicyDecision(event_id="evt_trace_2", proposed_intervention="retry_delayed", allowed=False, denial_reason="fraud_protection", policy_rules_applied=("fraud_protection",))`.
- **Downstream Selection**: `AllowedCandidates.allowed` is `()`, optimizer returns `selected_intervention="no_action"`, `selection_reason="no_allowed_candidate"`, and `BoundedExecutor` is never called (`execution_service.py:293-310`).

### Trace 3: Live Razorpay Webhook Where LLM Diagnoses `root_cause_category="fraud_suspect"` — **THE CRITICAL BYPASS BUG**
- **Inputs**:
  - `event`: Ingested via `POST /webhook/razorpay` (`event="payment.failed"`). `map_failed_payment_to_event()` (`failed_payment_ingestion.py:130`) hardcodes `risk_flag="normal"`.
  - `classification`: The LLM sees suspicious velocity/attributes and returns `root_cause_category="fraud_suspect"`, `candidate_interventions=("payment_link",)`.
  - `history`: Empty (`0` attempts, `False` duplicate, `0` spend).
- **Step-by-Step Evaluation (`policy.py:362-403`)**:
  1. Rule 1 (`fraud_protection`, `policy.py:362`): Checks `if event.risk_flag == "fraud_suspect":`. Since `event.risk_flag` is `"normal"`, **Rule 1 PASSES (`"fraud_check_passed"`)!**
  2. Rule 2 (`terminal_failure`, `policy.py:365`): Checks `if classification.root_cause_category == "terminal":`. Since `root_cause_category` is `"fraud_suspect"`, **Rule 2 PASSES (`"terminal_check_passed"`)!**
  3. Rules 3, 4, 5, 6 all pass!
- **Gate Output**: `PolicyDecision(allowed=True, denial_reason=None, proposed_intervention="payment_link")`!
- **Downstream Consequence**:
  - `RecoveryProbabilityEstimator` (`estimator.py:72-79`) applies `ROOT_CAUSE_ADJUSTMENT_BPS["fraud_suspect"]["payment_link"] = -4500`, reducing `payment_link` from `4800` bps to `300` bps (plus any positive adjustments, e.g. `+350` reliable customer + `+100` card = `750` bps).
  - For a ₹5,000 (`500_000` paise) payment, `EV = (500_000 * 750 // 10_000) - 100 - 500 = 3,750 - 600 = +3,150 paise`.
  - Because `"payment_link"` is the only allowed candidate, `EconomicInterventionOptimizer` selects `"payment_link"`, and **`BoundedExecutor` creates and sends a real Razorpay Payment Link to a payment the AI diagnosed as `fraud_suspect`!**

### Trace 4: Spend Cap Partial Filtering (`payment_link` Denied by Cost, `retry_delayed` Allowed at Zero Cost)
- **Inputs**:
  - `event`: `event_id="evt_trace_4"`, `failure_reason="expired_card"`, `risk_flag="normal"`, `amount_paise=200_000`.
  - `classification`: `root_cause_category="customer_action_needed"`, `candidate_interventions=("payment_link", "retry_delayed")`.
  - `history`: `existing_daily_spend_paise = 4_999_950` (only 50 paise remaining under the `5_000_000` paise daily cap).
- **Step-by-Step Evaluation (`policy.py:362-392`)**:
  - Candidate 1 (`"payment_link"`, cost = `100` paise):
    - Rules 1–5 pass (`fraud_check_passed`, `terminal_check_passed`, `duplicate_check_passed`, `retry_limit_passed`, `cooldown_check_passed`).
    - Rule 6 (`spend_cap_exceeded`, `policy.py:387-391`): `4_999_950 + 100 = 5_000_050 > 5_000_000` -> **DENIED** with `denial_reason="spend_cap_exceeded"`, `policy_rules_applied=(5 pass markers..., "spend_cap_exceeded")`.
  - Candidate 2 (`"retry_delayed"`, cost = `0` paise):
    - Rules 1–5 pass.
    - Rule 6 (`spend_cap_exceeded`): `4_999_950 + 0 = 4_999_950 <= 5_000_000` -> **ALLOWED**!
- **Downstream Consequence**:
  - `AllowedCandidates` filters the candidate set to `allowed=("retry_delayed",)` (`optimizer.py:255-267`).
  - Even though `"payment_link"` has a much higher recovery probability on `expired_card`, it is excluded from economic evaluation and `"retry_delayed"` is selected.

### Trace 5: Unconfigured Razorpay Client (`configuration_missing`) Followed by Immediate Retry — **THE HISTORY POISONING BUG**
- **Inputs**:
  - `event`: `event_id="evt_trace_5"`, `customer_id="cust_05"`, `risk_flag="normal"`.
  - `classification`: `root_cause_category="customer_action_needed"`, `candidate_interventions=("payment_link",)`.
  - First call at `t0 = 13:00:00Z`: `execute_event()` runs with `razorpay_client=None` (keys not configured yet).
- **Step-by-Step Execution at `t0 = 13:00:00Z`**:
  1. `PolicyEngine.evaluate()` returns `allowed=True` for `"payment_link"`.
  2. `BoundedExecutor.execute()` returns `ExecutionOutcome(status="FAILED", detail="configuration_missing: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required...")` (`executor.py:232-246`).
  3. `execution_service.py:447-455` inserts `InterventionAttempt(event_id="evt_trace_5", intervention="payment_link", customer_id="cust_05", cost_paise=100, attempted_at="2026-08-27T13:00:00+00:00", status="failed")`.
  4. `execution_service.py:475` deletes the `execution_claims` row so the operator can retry after setting `RAZORPAY_KEY_ID`.
- **Second Call at `t1 = 13:05:00Z` (Operator sets `RAZORPAY_KEY_ID` and clicks "Execute Recommended Action" 5 minutes later)**:
  1. `db.get_policy_history()` (`db.py:638-673`) reads the `status="failed"` row from `t0` and returns:
     - `customer_intervention_count_24h = 1`
     - `most_recent_event_intervention_time = 2026-08-27T13:00:00+00:00`
     - `has_successful_intervention = False`
     - `existing_daily_spend_paise = 100`
  2. `PolicyEngine.evaluate()` at `13:05:00Z`:
     - Rules 1–4 pass (`customer_intervention_count_24h` is `1 < 2`).
     - Rule 5 (`event_cooldown_active`, `policy.py:377-384`): `elapsed = 5 minutes < 30 minutes` -> **DENIED with `event_cooldown_active`!**
  3. If the operator tried twice before setting the env var, `customer_intervention_count_24h` is `2`, and the customer is **blocked for 24 hours with `customer_intervention_limit_exceeded`** even though zero external actions ever occurred!

### Trace 6: Exact Cooldown Boundary (`elapsed == 30 minutes`) — Inclusive Allow
- **Inputs**:
  - `event`: `event_id="evt_trace_6"`, `customer_id="cust_06"`, `risk_flag="normal"`.
  - `history`: 1 prior failed attempt at `12:30:00+00:00`, `evaluation_time = 13:00:00+00:00` (`elapsed = 30m 00s`), `config.event_cooldown_minutes = 30`.
- **Step-by-Step Evaluation (`policy.py:377-385`)**:
  - `elapsed = evaluation_time - recent = timedelta(minutes=30)`.
  - Checks `if elapsed < timedelta(minutes=config.event_cooldown_minutes):` (`30m < 30m` is `False`).
  - Appends `"cooldown_check_passed"` and **ALLOWS** the intervention at the exact 30-minute boundary.
