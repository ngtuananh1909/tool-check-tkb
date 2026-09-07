# eLearning Playwright Migration Plan

## 1. Executive Decision

This document establishes the final hardened architecture, network prerequisites, data contracts, and verification criteria for **re-architecting the TDTU eLearning deadline crawler on a pure Playwright DOM automation pipeline**, while **preserving all Google Calendar reconciliation safety guarantees introduced in PR #23 (`feature/elearning-calendar-api-v2`)**.

### Primary Investigation Findings

1. **The Existing State Fact**: On `main` (`72f2be1`), eLearning deadlines are already collected using Playwright via `crawler.fetch_elearning_deadlines()`. However, the `main` implementation is critically unsafe for authoritative Calendar reconciliation:
   - It performs sequential, multi-page DOM crawling across all enrolled courses and incomplete activities (estimated 25–40 full navigations per run), resulting in severe latency (measured 75.8s in CI before timeout) and frequent failures.
   - It **silently swallows course-level timeouts** inside `_collect_elearning_course_deadlines()`, returning a partial list of deadlines as a complete success.
   - On `main`, `calendar_sync.py` has **no deadline authority window** and performs global, unbounded deletions. Consequently, a partial crawl on `main` causes Google Calendar to delete valid deadlines from courses that timed out.
2. **The Safety Revolution in PR #23**: PR #23 introduced essential, domain-level safety invariants that must be preserved:
   - Mandatory pairing of `deadlines` with `deadline_window: tuple[datetime, datetime]` (reconciliation fails closed if `deadlines` is a list without a window).
   - Strict half-open authority window semantics `[window_start, window_end)`.
   - Google Calendar deletion scope strictly restricted to the half-open window; out-of-horizon deadlines are preserved.
   - Existing Calendar events with unparsable start times are preserved rather than deleted on uncertainty.
   - Explicit distinction between `deadlines = None` (crawl failed -> skip reconciliation) and `deadlines = []` (authoritative empty crawl -> reconcile window).
   - End-to-end timezone-aware datetimes pinned to `Asia/Ho_Chi_Minh`.
   - Stable Moodle event identity via `source_signature = moodle_event:<id>`.
3. **The Network Reality Check**:
   - In GitHub Actions run `34026928528` (`main`, Playwright), `page.goto("https://elearning.tdtu.edu.vn/login/index.php")` timed out after 60,000 ms.
   - In GitHub Actions run `33946292754` (PR #23, HTTP `requests.Session`), connecting to `https://elearning.tdtu.edu.vn/login/index.php` failed with `connect timeout` across all 3 retries (33s total).
   - Observed behavior is strongly consistent with Azure/GitHub-hosted-runner IP filtering, routing filtering, or another network-layer reachability restriction. The exact network-layer cause has not been independently proven via packet capture.
   - Both HTTP and browser automation use the same egress IP address on Azure-hosted GitHub Actions runners. Playwright does not change the runner's egress IP; therefore, switching `requests` -> Chromium does not by itself solve the production GitHub Actions reachability problem.
4. **Final Targeted DOM Discovery Findings (2026-09-07)**:
   - **Stable Identity**: 5/5 inspected event cards on Moodle Calendar (`/calendar/view.php`) contained explicit numeric `data-event-id` attributes, providing direct access to the stable Moodle event ID without mutable title hashing.
   - **Actionable Semantics for Assignments**: Moodle Calendar views display all course syllabus deadlines regardless of individual student submission state. However, on Assignment activity pages (`/mod/assign/view.php`), student submission status is marked by a stable, non-localized CSS class: `td.submissionstatussubmitted` in `table.submissionstatustable`.
   - **Quiz & Generic Completion Boundaries**: In the student's active live horizon, no active quiz close deadline exists to prove live attempt state classes, and generic activities lack stable completion classes in this Moodle theme. Therefore, v1 strictly limits supported deadline modules to `/mod/assign/`. Any candidate deadline from an unsupported module kind in the crawled authority window **fails closed** (`ElearningCrawlError`), ensuring partial results never escape.
   - **Authoritative Horizon**: Month view (`/calendar/view.php?view=month&time=<timestamp>`) allows direct, deterministic URL navigation across calendar months, proving complete event coverage for the current month and subsequent months without fragile click loops.

### Architectural Recommendation & Approval Status

- **Architecture Approval**: **APPROVED**. The pure Playwright DOM-based architecture is mathematically sound, preserves PR #23's calendar reconciliation safety guarantees, resolves the partial-crawl bugs of `main`, and enforces a strict supported-type contract.
- **Implementation Approval**: **APPROVED**. Implementation code may be developed and tested against synthetic fixtures and local environments.
- **Production Deployment Approval**: **BLOCKED BY GATE F**. Production deployment on GitHub-hosted runners cannot proceed until an alternate egress environment (Gate 0) is provisioned.

---

## 2. Explicitly Separating Three Independent Questions

To prevent architectural confusion, this plan treats three core engineering questions as strictly independent:

```text
┌────────────────────────────────────────────────────────────────────────┐
│ 1. Crawler Correctness                                                 │
│    Can Playwright DOM reliably extract the correct actionable          │
│    deadlines and stable event identities from Moodle UI?               │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ (Does NOT imply)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. Reconciliation Safety                                               │
│    Can Google Calendar safely reconcile crawler output within a        │
│    bounded [window_start, window_end) without over-deleting?           │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ (Does NOT imply)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. Deployment Reachability (Gate 0)                                    │
│    Can the production execution environment reach                      │
│    https://elearning.tdtu.edu.vn at the network layer?                 │
└────────────────────────────────────────────────────────────────────────┘
```

Passing unit tests or parser fixtures for Question 1 or Question 2 provides **zero evidence** that Question 3 is resolved. Deployment requires independent verification of all three.

---

## 3. Revalidated Repository State

Repository state was revalidated directly via Git and GitHub CLI on **2026-09-07 at 08:13:04+07:00**:

| Reference | Commit SHA | State / Relationship | Validation Source |
| :--- | :--- | :--- | :--- |
| `origin/main` | `72f2be14f397a0dc34ed93473c75633a54de6037` | Production base branch | `git rev-parse origin/main` |
| `PR #23` (`feature/elearning-calendar-api-v2`) | `7599dba4903edc0c4fc63b6adeab5e2035687f14` | Open PR (8 commits ahead of `main`) | `gh pr view 23` |
| Merge Base | `72f2be14f397a0dc34ed93473c75633a54de6037` | Diverged cleanly from `main` HEAD | `git merge-base main HEAD` |
| Mergeable State | `MERGEABLE` | Clean mergeable status against `main` | `gh pr view 23 --json mergeable` |

### Knowledge Graph Exploration Evidence

Per repository instructions in `CLAUDE.md`, the internal SQLite knowledge graph (`.code-review-graph/graph.db`, containing 449 nodes, 4,680 edges, 37 execution flows, and 13 architectural communities) was re-queried:
- **Flow 1165** (`fetch_elearning_deadlines`): Verified node path 1521 -> 1522 -> 1523 -> 1492 -> 1495 -> 1525 -> 1524 -> 1528.
- **Flow 1171** (`_parse_elearning_deadlines_on_course`): Verified sub-flow through nodes 1526, 1530, 1527, and 1528.
- **Flow 1139** (`sync_crawled_data_to_google_calendar`): Confirmed invocation of `_replace_bot_events_for_range` (node 1121).

### Three-Way State Comparison Matrix

| Concern | Legacy Playwright (`main`) | PR #23 Moodle API Client | Proposed Playwright DOM Target |
| :--- | :--- | :--- | :--- |
| **Data Source Transport** | Playwright Chromium (multi-page course scrape) | Pure HTTP `requests.Session` (`core_calendar_...`) | Playwright Chromium inspecting rendered Moodle DOM only |
| **Authentication** | Browser form login (`#loginbtn`) | Form `logintoken` + HTTP POST + `sesskey` | Browser form login (`form#login input:not([type='hidden'])`) |
| **Deadline Identity** | Weak: `activity_url` fallback to course/title hash | Strong: `moodle_event:<id>` via API JSON | Strong: `moodle_event:<id>` via DOM `data-event-id` |
| **Crawl Horizon** | Unbounded / undefined | Authoritative 120 days | Proven bounded window (Current + Next Month, 45–60d) |
| **Partial-Failure Semantics** | **UNSAFE**: Swallows course timeouts; returns partial list | **SAFE**: Fails closed to `deadlines = None` | **SAFE**: Fails closed to `deadlines = None` |
| **Calendar Authority Window** | None (global deletion of unlisted events) | Strict half-open `[window_start, window_end)` | Strict half-open `[window_start, window_end)` |
| **Mandatory Window Guard** | None | Raises `ValueError` if list passed without window | Raises `ValueError` if list passed without window |
| **Preserve Unparsable Events** | Deletes them | Preserves existing events with unparsable start | Preserves existing events with unparsable start |
| **Timezone Contract** | Naive datetimes mixed with manual replace | Timezone-aware (`Asia/Ho_Chi_Minh`) end-to-end | Timezone-aware (`Asia/Ho_Chi_Minh`) end-to-end |
| **GitHub Actions Reachability** | Connection timeout (60s) on Azure runners | Connection timeout (33s) on Azure runners | Fails without Gate 0 (requires alternate egress) |

---

## 4. Current Main Execution Flow

On current `main`, `run_hour.py` executes:

```text
run_hour.py:145
  │
  └─► crawler.fetch_elearning_deadlines() [crawler.py:1634]
        │
        ├──► _launch_chromium(playwright) [crawler.py:1492]
        ├──► _login_and_open_elearning_dashboard(page, user, pwd) [crawler.py:141]
        │      ├──► page.goto("https://elearning.tdtu.edu.vn/login/index.php", timeout=60000)
        │      ├──► page.fill(username), page.fill(password)
        │      ├──► page.locator(submit).first.click(timeout=10000)
        │      ├──► page.wait_for_url(..., timeout=30000)
        │      ├──► page.goto("https://elearning.tdtu.edu.vn/my/", timeout=60000)
        │      └──► page.wait_for_selector(ELEARNING_SELECTOR_DASHBOARD_READY, timeout=20000)
        │
        ├──► _parse_elearning_dashboard_deadlines(page) [crawler.py:1710]
        ├──► _parse_elearning_courses(page) [crawler.py:1757]
        ├──► _collect_elearning_course_deadlines(page, courses) [crawler.py:1666]
        │      │   Iterates over each discovered course:
        │      ├──► page.goto(course_url, timeout=60000)
        │      │      [CRITICAL FLAW: catches PlaywrightTimeoutError, logs warning, and CONTINUES]
        │      └──► _parse_elearning_deadlines_on_course(page, course) [crawler.py:1768]
        │             ├──► Scans img[alt*="Not completed"]
        │             └──► page.goto(activity_url, timeout=60000) for each incomplete activity
        │
        └─► _deduplicate_elearning_deadlines(...) [crawler.py:1684]
              └─► Returns list[dict]
```

---

## 5. PR #23 Execution Flow & Reusable Safety Concepts

PR #23 replaced the Playwright crawler with an HTTP Moodle API client. While the transport layer is being changed back to Playwright DOM, PR #23's safety architecture must be ported:

```text
Reusable Safety Concept                          Classification   Rationale
────────────────────────────────────────────────────────────────────────────────────────────────────────
DeadlineCrawlResult dataclass                    KEEP             Packages items with window_start and window_end
Strict half-open interval [start, end)           KEEP             Eliminates boundary ambiguity and dual-ownership
Mandatory deadline_window presence guard         KEEP             Reconciliation fails closed if window missing
None vs [] tri-state semantic contract          KEEP             Guarantees failed crawls never delete calendar events
Preserve unparsable Google Calendar starts       KEEP             Never delete on uncertainty
Timezone-aware datetimes (Asia/Ho_Chi_Minh)      KEEP             Eliminates naive/aware comparison crashes
Whole-second boundary precision                  KEEP             Prevents microsecond rounding mismatches
source_signature = moodle_event:<id>             KEEP             Stable Moodle event identity
Pure HTTP Moodle API client (elearning/client)   DROP             Replaced by pure Playwright DOM crawler
Regression tests in test_calendar_sync.py        KEEP             Maintains verification of calendar deletion safety
```

---

## 6. Historical GitHub Actions Evidence & Network Analysis

### Empirical Run Log Comparison

| Metric / Event | Run `34026928528` (`main`) | Run `33946292754` (PR #23) |
| :--- | :--- | :--- |
| **Commit SHA** | `72f2be1` | `7599dba` |
| **Client Transport** | Playwright Chromium | Python `requests.Session` |
| **Target URL** | `https://elearning.tdtu.edu.vn/login/index.php` | `https://elearning.tdtu.edu.vn/login/index.php` |
| **Observed Failure** | `playwright._impl._errors.TimeoutError: Page.goto: Timeout 60000ms exceeded.` | `Failed to load login page: connect timeout` (3 retries, 33s total) |
| **Portal Schedule Crawl** | **SUCCESS** (34 rows scraped from `old-stdportal.tdtu.edu.vn`) | **SUCCESS** (34 rows scraped from `old-stdportal.tdtu.edu.vn`) |
| **Runner Environment** | GitHub-hosted `ubuntu-latest` (Azure IP) | GitHub-hosted `ubuntu-latest` (Azure IP) |

### Network Hypothesis Evaluation

- **Hypothesis A (Network-layer IP or routing restriction on Azure/GitHub runner IPs)**:
  - **Confidence**: **SUPPORTED**. Both `requests` and Chromium fail at the initial TCP connection phase before any TLS negotiation or HTTP headers are exchanged. The student portal on a different subdomain succeeds, indicating domain-specific network filtering.
- **Hypothesis B (WAF browser-vs-requests fingerprint discrimination)**:
  - **Confidence**: **UNLIKELY**. If WAF was blocking based on User-Agent or TLS JA3 fingerprint, it would complete the TCP handshake and return HTTP `403 Forbidden` or a Cloudflare challenge page. Instead, both clients experience a TCP connect timeout.
- **Hypothesis C (Unproven edge network causes)**:
  - **Confidence**: **UNPROVEN**. Specific firewall rule triggers (e.g. drop vs reject, ASN blocklist vs rate-limiting) cannot be proven without packet capture or server-side firewall telemetry.

### Gate 0: Deployment Prerequisite

> [!CAUTION]
> **GATE 0 PREREQUISITE**:
> Migrating from HTTP to Playwright does **not** change the runner's egress IP.
> Production deployment on GitHub-hosted runners will remain non-viable until an alternate egress environment is provisioned:
> 1. Self-hosted runner hosted on a reachable network (e.g. domestic Vietnam IP).
> 2. Trusted proxy / VPN configured at runner or browser level.
> 3. Dedicated VPS located in Vietnam.

---

## 7. DOM Discovery Evidence (Aggregate & Synthetic)

The following empirical evidence was directly collected via a safe, read-only Playwright probe against `https://elearning.tdtu.edu.vn` on **2026-09-06 and 2026-09-07**:

| Claim | Observed Page | Observed Selector / Attribute | Sanitized Structural Example | How Observed | Date | Confidence | Authority Impact |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Login Username Selector** | `/login/index.php` | `form#login input[name="username"]:not([type="hidden"])` | `input[name="username"]` | Live probe (failed on hidden guest input, succeeded with `:not([type="hidden"])`) | 2026-09-06 | **HIGH (OBSERVED)** | Required for crawler login |
| **Stable Event ID** | `/calendar/view.php?view=upcoming` | `.event.m-t-1[data-event-id]` | `data-event-id="100001"` | Live probe (5/5 event cards contained numeric ID) | 2026-09-06 | **HIGH (OBSERVED)** | **Mandatory** for stable Google Calendar identity |
| **Stable Course ID** | `/calendar/view.php?view=upcoming` | `.event.m-t-1[data-course-id]` | `data-course-id="50001"` | Live probe (5/5 event cards contained numeric ID) | 2026-09-06 | **HIGH (OBSERVED)** | Required for course grouping |
| **Due Timestamp** | `/calendar/view.php?view=upcoming` | `a[href*="view=day"][href*="time="]` | `href="...view=day&time=1788973200"` | Live probe (extracted Unix timestamp integer) | 2026-09-06 | **HIGH (OBSERVED)** | Eliminates fragile date string parsing |
| **Activity Link** | `/calendar/view.php?view=upcoming` | `a.card-link[href*="/mod/"]` | `href=".../mod/assign/view.php?id=900001"` | Live probe (found on card footer) | 2026-09-06 | **HIGH (OBSERVED)** | Links event to Moodle activity |
| **Calendar Completion Absence** | `/calendar/view.php?view=upcoming` | N/A (No completion attributes found) | N/A | Live probe (verified submitted activity remained visible on calendar) | 2026-09-06 | **HIGH (OBSERVED ABSENCE)** | Calendar DOM alone omits submission status |
| **Assignment Submission Status** | `/mod/assign/view.php?id=...` | `table.submissionstatustable td.submissionstatussubmitted` | `class="submissionstatussubmitted cell c1 lastcol"` | Live probe (confirmed presence of stable non-localized class) | 2026-09-07 | **HIGH (OBSERVED)** | Proves Policy B actionable detection for assignments |
| **Month View Direct Navigation** | `/calendar/view.php?view=month` | URL query parameter `time=<first_of_month>` | `https://.../calendar/view.php?view=month&time=1790787600` | Live probe (verified October month table rendered directly with 0 events) | 2026-09-06 | **HIGH (OBSERVED)** | Month-by-month navigation proves multi-month horizon |

---

## 8. Making the Architecture Target Unambiguous

### Approved Target: Pure Playwright DOM-Based Crawler

The approved target is strictly a **DOM-based browser crawler**:
- The browser logs in using form inputs.
- The browser navigates to Moodle Calendar month view pages (`/calendar/view.php?view=month&time=<timestamp>`).
- The crawler inspects rendered HTML elements, reads DOM attributes (`data-event-id`, `data-course-id`), extracts activity links, and navigates only to candidate activity pages within the authority window to verify completion status.
- The crawler must **NOT** invoke Moodle's internal AJAX services (`/lib/ajax/service.php` or `core_calendar_get_action_events_by_timesort`) as an automatic primary or fallback data source.

### Rejected / Out-of-Scope Alternatives

> [!IMPORTANT]
> **IN-PAGE AJAX EVALUATION IS OUT OF SCOPE**:
> Executing `page.evaluate(() => fetch("/lib/ajax/service.php", ...))` inside the browser session is **explicitly rejected** for this migration.
> **Rationale**: It reintroduces the Moodle internal AJAX API dependency that this task was directed to replace. Any future hybrid API approach requires separate explicit senior approval.

---

## 9. Stable Identity Strategy

### Decision: Option A (Required Stable Identity)

```text
source_signature = f"moodle_event:{moodle_event_id}"
```

1. **Mandatory Rule**: If an event rendered in the DOM lacks a valid numeric `data-event-id`, the crawler **MUST FAIL CLOSED**:
   ```python
   raise ElearningCrawlError(f"calendar event card #{card_idx} missing required data-event-id")
   ```
   The orchestrator catches this exception, sets `deadlines = None`, and preserves all Google Calendar events.
2. **Rejection of Mutable Fallback**: Generating a signature from `hash(course_id, activity_name, event_kind, activity_url)` is **strictly forbidden** for authoritative reconciliation:
   - Activity titles frequently change when instructors fix typos or update assignments.
   - Activity URLs collide when an activity has multiple calendar events (e.g. open and close).
   - Theme changes or localization shifts would alter the hash, causing catastrophic deletion of existing Google Calendar events.

---

## 10. Actionable / Completion Semantics (Resolving Gate B)

### Module / Actionability Support Matrix

| Module / Event Kind | Candidate Detection | Completion / Actionable Detector | Observed? | Supported in v1? | Failure Behavior |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Assignment Due** (`/mod/assign/`) | Calendar card links to `/mod/assign/` and matches due semantic | Navigate to activity URL; check for `table.submissionstatustable td.submissionstatussubmitted` | **YES** (`td.submissionstatussubmitted` verified live) | **YES (SUPPORTED)** | Missing submission table or navigation timeout -> **FAIL CLOSED** (`ElearningCrawlError`) |
| **Quiz Close** (`/mod/quiz/`) | Calendar card links to `/mod/quiz/` and matches close semantic | Attempt state verification (`table.quizattemptsummary` vs start attempt form) | **NO** (No active quiz close deadline in live student horizon) | **NOT SUPPORTED IN V1** | If encountered in authority window -> **FAIL CLOSED** (`ElearningCrawlError`) |
| **Generic Completion** (`should-be-completed`) | Calendar card matches `should be completed` / `cần hoàn thành` | Activity completion tracking controls | **NO** (No stable completion classes rendered on live theme) | **NOT SUPPORTED IN V1** | If encountered in authority window -> **FAIL CLOSED** (`ElearningCrawlError`) |
| **Open Event** (`bắt đầu` / `opens`) | Title matches `bắt đầu` or `opens` | N/A (Lifecycle opening time, not a deadline) | **YES** (Verified live on quiz opening event) | **EXCLUDE** | Excluded from deadline list; does not fail crawl |
| **Unknown / Unrecognized** | Event not matching recognized patterns | N/A | N/A | **NOT SUPPORTED** | **FAIL CLOSED** (`ElearningCrawlError`) |

### Strict Supported-Type Contract

To prevent architectural drift and guarantee reconciliation safety:
1. **v1 Scope Boundary**: v1 strictly claims support for **Assignment Due (`/mod/assign/`)** events, where the DOM actionable contract (`td.submissionstatussubmitted`) is 100% verified.
2. **Fail-Closed on Unsupported Candidates**: If the crawler encounters a candidate deadline in the authority window belonging to an unsupported module kind (such as `/mod/quiz/` or generic completion), it **MUST NOT** silently skip it. Silently dropping an unverified deadline would cause Google Calendar reconciliation to delete the corresponding event. Instead, the crawler raises `ElearningCrawlError`, setting `deadlines = None` and preserving all Google Calendar events.

### Policy Comparison & Final Recommendation

```text
Policy                           Mechanism                           Performance & Complexity          Reconciliation Impact
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Policy A — Syllabus Deadlines    Scrape candidate deadlines from     Extremely fast: 1 login + 2 month Submitted assignments remain
                                 Calendar month DOM only. Sync all   pages = 3 navigations total.      visible on Google Calendar
                                 future deadlines until due date.    Zero per-activity page visits.    until their due date elapses.
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Policy B — Actionable Deadlines  Scrape candidate deadlines from     Bounded overhead: 1 login + 2     Preserves PR #23 behavior:
(ADOPTED FOR V1)                 month DOM; for the K candidates     month pages + K activity visits   only incomplete work requiring
                                 inside [start, end), visit each     (K is typically 2–6 in window)    action is synced. Completed
                                 activity URL to check completion    = 5–9 navigations (12–18s total). items are excluded.
                                 via td.submissionstatussubmitted.
```

**Recommendation**: **Adopt Policy B for `/mod/assign/`**.
- Preserves PR #23's actionable-deadline product contract for assignments.
- Adds only 2–6 targeted navigations per run (12–18s runtime), avoiding the 30+ sequential loads of `main`.
- Supported by direct empirical observation of `td.submissionstatussubmitted`.

---

## 11. Event-Kind Structural Identification

| Event Semantic | Primary Structural Indicator | Secondary DOM / Title Match | Action | Confidence |
| :--- | :--- | :--- | :--- | :--- |
| **`due`** | Activity URL contains `/mod/assign/` | Title matches `is due` or `đến hạn` / `hết hạn` | **INCLUDE** (subject to submission check) | **HIGH (OBSERVED)** |
| **`close`** | Activity URL contains `/mod/quiz/` | Title matches `closes` or `đóng` / `kết thúc` | **FAIL CLOSED** (unsupported in v1) | **HIGH (POLICY)** |
| **`should-be-completed`** | Activity URL contains `/mod/` | Title matches `should be completed` or `cần hoàn thành` | **FAIL CLOSED** (unsupported in v1) | **HIGH (POLICY)** |
| **`open`** | Event title matches `bắt đầu` / `opens` | Title regex `(?i)(bắt đầu|opens)` | **EXCLUDE** (known non-deadline) | **HIGH (OBSERVED)** |
| **`unknown`** | Unrecognized structure or pattern | N/A | **FAIL CLOSED** (raise `ElearningCrawlError`) | **HIGH (POLICY)** |

### Priority of Detection:
1. **Module URL Component**: Extracting `/mod/assign/` vs other modules establishes the core activity type.
2. **Bilingual Title Pattern Matching**: Recognized English and Vietnamese lifecycle phrases.
3. **Fail-Closed Rule**: Any calendar event in the authority window that cannot be unambiguously classified into a supported kind raises `ElearningCrawlError`.

---

## 12. Authoritative Window & Crawl Completeness (Resolving Gate C)

### Proven Horizon via Deterministic Month Navigation

Instead of relying on the upcoming view (which is site-limited to ~14 days), the crawler uses **deterministic month navigation**:
- **Target URL Pattern**: `https://elearning.tdtu.edu.vn/calendar/view.php?view=month&time=<unix_timestamp>`
- Passing the Unix timestamp for the 1st day of a month directly loads that full calendar month.
- Live probe confirmed direct URL navigation to October (`time=1790787600`) rendered the complete October calendar table without relying on client-side click events.

### Exact Authority Window Definition: Current Month + Next Month

To ensure a mathematically clean exclusive boundary without 23:59:59 second-clamping bugs, the authority window is defined as the half-open interval:
```text
[window_start, window_end)
```
- **`window_start`**: Current whole second in `Asia/Ho_Chi_Minh`:
  ```python
  window_start = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(microsecond=0)
  ```
- **`window_end`**: Exact `00:00:00` on the **FIRST DAY OF THE MONTH AFTER the final crawled month**:
  ```python
  # Example: If crawling September 2026 and October 2026:
  # window_start = 2026-09-07T08:13:00+07:00
  # window_end   = 2026-11-01T00:00:00+07:00
  ```
- **Pages Traversed**: Exactly 2 month pages (Month 0: current month, Month 1: next month).
- **Proven Horizon**: 45 to 60 days of guaranteed complete calendar coverage.

### Month Page Completeness Verification

An empty month (0 events rendered) is considered authoritatively valid **ONLY** after the month page structure itself is verified:
1. Authenticated session confirmed valid (URL did not redirect to `/login/index.php`).
2. The month heading (`.calendar-controls h2` or `h2.current`) matches the expected month name and year.
3. The calendar root table (`table.calendarmonth`) is present and fully rendered.
4. If a month page loads with 0 events but fails any of checks 1–3, it is treated as a **crawl failure** (`raise ElearningCrawlError`), never an authoritative empty month.

### Partial Month & Window Boundary Filtering

Because month pages cover the entirety of both calendar months, they will contain events that fall outside the half-open interval:
- Past events in the current month (`event_due < window_start`).
- Future events at or after `window_end` (`event_due >= window_end`).

**Mandatory Boundary Invariant**:
The crawler must parse all candidate events but filter normalized authoritative items strictly to:
```python
window_start <= item["due_date_dt"] < window_end
```
Events with `item["due_date_dt"] == window_end` are **strictly excluded** from the current window. The exact same `(window_start, window_end)` pair is returned in `DeadlineCrawlResult` and passed to Google Calendar reconciliation.

---

## 13. Failure Semantics & Mandatory Tri-State Contract

The tri-state failure contract must be strictly maintained across the boundary between crawler, orchestrator, and calendar sync:

```text
Crawl Result State         deadlines Value   deadline_window Value   Calendar Sync Action
──────────────────────────────────────────────────────────────────────────────────────────────────
Crawl Failed               None              None                    SKIP reconciliation (0 deletions)
Complete Empty Crawl       []                (start, end)            RECONCILE [start, end) (delete stale)
Complete Crawl with Items  [item1, ...]      (start, end)            RECONCILE [start, end) (upsert & delete)
Invalid State 1            [...]             None                    FAIL CLOSED (ValueError)
Invalid State 2            None              (start, end)            FAIL CLOSED (ValueError)
Invalid State 3            [...]             start >= end            FAIL CLOSED (ValueError)
Invalid State 4            [...]             naive datetime          FAIL CLOSED (ValueError)
```

### Critical Invariant: No Partial Return

Under no circumstances may a failed or interrupted crawl return a partial list of items. If any navigation or candidate verification fails, raise `ElearningCrawlError`, setting `deadlines = None`.

---

## 14. Hardened Google Calendar Reconciliation Contract

In `calendar_sync.py`:

```python
def sync_crawled_data_to_google_calendar(
    class_sessions: list[dict] | None,
    exams: list[dict] | None,
    student_id: str | None = None,
    deadlines: list[dict] | None = None,
    deadline_window: tuple[datetime, datetime] | None = None,
) -> tuple[str, bool]:
    # 1. Hardened Contract Validation (FAIL CLOSED)
    if deadlines is not None:
        if deadline_window is None:
            raise ValueError("deadlines list provided without authoritative deadline_window; sync aborted")
        if not isinstance(deadline_window, tuple) or len(deadline_window) != 2:
            raise ValueError("deadline_window must be a 2-tuple of (start, end)")
        w_start, w_end = deadline_window
        if not isinstance(w_start, datetime) or not isinstance(w_end, datetime):
            raise ValueError("deadline_window elements must be datetime instances")
        if w_start.tzinfo is None or w_end.tzinfo is None:
            raise ValueError("deadline_window elements must be timezone-aware")
        if w_start >= w_end:
            raise ValueError(f"deadline_window start ({w_start}) must be strictly before end ({w_end})")
            
    if deadlines is None and deadline_window is not None:
        raise ValueError("deadline_window provided while deadlines is None")
```

Inside `_replace_bot_events_for_range`:

```python
if event_source_type == SYNC_SOURCE_DEADLINE:
    if deadline_window is None:
        logger.warning("Skipping deadline deletion because deadline_window is missing.")
        continue
    window_start, window_end = deadline_window
    event_start_dt = _parse_calendar_event_start(event)
    
    # 2. Preserve unparsable events
    if event_start_dt is None:
        logger.warning("Preserving deadline event with unparsable start time: %s", source_key)
        continue
        
    # 3. Preserve events outside [window_start, window_end)
    if not (window_start <= event_start_dt < window_end):
        logger.debug("Preserving Google deadline event outside sync window: %s", source_key)
        continue

# 4. Safe Delete inside window
_safe_delete_calendar_event(service, calendar_id, event_id)
```

---

## 15. Safe Operational Rollback Plan

### Rejection of Legacy Crawler Rollback

Reverting to `main`'s legacy crawler (`crawler.fetch_elearning_deadlines`) is **unacceptable as an emergency rollback target** because it is already known to have partial-crawl swallowing bugs and unbounded global Calendar deletion.

### Primary Rollback Strategy: Fail-Safe Preserve

If the new Playwright crawler experiences unpredicted errors in production:
1. The orchestrator catches crawler exceptions in `run_hour.py` and defaults to:
   ```python
   deadlines = None
   deadline_window = None
   ```
2. Google Calendar reconciliation **skips deadline deletion entirely**, preserving all existing Calendar events.
3. Schedule and exam crawling from the TDTU portal continues without interruption.
4. No complex runtime feature flags are required.

---

## 16. Privacy Sanitization & Production Logging Hygiene

1. **Strict Credential Redaction**: `STUDENT_ID` and `PASSWORD` must never be logged or echoed in exceptions.
2. **Sanitized Exception Messages**: Exception designs must use structural descriptions rather than raw HTML or identifiers:
   ```python
   # PROHIBITED:
   # raise ElearningCrawlError(f"... {card_html[:100]}")
   
   # APPROVED:
   raise ElearningCrawlError(f"calendar event card #{card_idx} missing required data-event-id")
   ```
3. **No Personal or Course Identifiers in Error Logs**: Logs and exceptions must strictly avoid printing real student names, course titles, or assignment descriptions.
4. **Extended URL Query Sanitization**: The URL sanitizer must strip all query parameters matching `(?i)(token|sesskey|logintoken|session|pass|pwd|key)`.
5. **No Production Screenshots or Artifacts**: No screenshots, trace ZIPs, HAR logs, or raw HTML dumps may be written to disk or CI artifacts in production.
6. **Synthetic Test Fixtures**: All unit tests committed to the repository must use synthetic course IDs (`"50001"`), synthetic names (`"Synthetic Assignment A"`), and dummy URLs.

---

## 17. File-Level Change Plan

Following the `CLAUDE.md` **Simplicity First** principle, code changes are concentrated in minimal files:

```text
elearning/
    ├── __init__.py           # Public exports: PlaywrightElearningCrawler, DeadlineCrawlResult, exceptions
    ├── crawler.py            # Browser lifecycle, login, Calendar Month navigation, candidate activity inspection
    ├── mapper.py             # Event dictionary normalization, event-kind policy, timezone localization
    └── exceptions.py         # Minimal typed exceptions (ElearningError, ElearningAuthError, ElearningCrawlError)
calendar_sync.py              # Window validation and [start, end) deletion boundaries
run_hour.py                   # Orchestration: invokes crawler, passes window to calendar_sync
crawler.py                    # Clean up: delete legacy scraper functions
tests/
    ├── test_elearning_crawler.py  # Playwright fixture and parser tests
    └── test_calendar_sync.py      # Window validation and boundary deletion tests
```

---

## 18. Testing Strategy

Unit tests in `tests/test_elearning_crawler.py` and `tests/test_calendar_sync.py` must assert:

```text
Test Identifier                                  Assertion / Scenario
────────────────────────────────────────────────────────────────────────────────────────────────────────
test_missing_stable_event_id_fails_closed        DOM event lacking data-event-id raises ElearningCrawlError
test_unknown_event_kind_fails_closed             Unrecognized event title/kind raises ElearningCrawlError
test_unsupported_deadline_module_fails_closed    Candidate deadline for /mod/quiz/ or generic raises ElearningCrawlError
test_upcoming_view_parser_extracts_timestamp     Extracts Unix timestamp from href 'view=day&time=...'
test_event_kind_filtering_excludes_open          Events matching 'bắt đầu' or 'open' are discarded
test_activity_submission_detection_submitted    td.submissionstatussubmitted causes candidate to be excluded
test_activity_submission_detection_unsubmitted  Assignment lacking submitted class is retained as deadline
test_window_filtering_excludes_past_month_events Events before window_start are excluded from crawl result
test_window_filtering_boundary_at_window_end    Event exactly at window_end (first day of month 00:00:00) is excluded
test_window_filtering_boundary_before_window_end Event 1 second before window_end is included
test_empty_expected_month_authoritative_success  Valid month table with 0 events returns authoritative empty list
test_empty_malformed_month_fails_closed          Missing month table or login redirect raises ElearningCrawlError
test_window_validation_rejects_missing_window    deadlines list with deadline_window=None raises ValueError
test_window_validation_rejects_inverted_range    deadline_window with start >= end raises ValueError
test_window_validation_rejects_naive_datetime    deadline_window with naive datetime raises ValueError
test_window_validation_rejects_none_with_window  deadlines=None with non-None window raises ValueError
test_calendar_sync_preserves_unparsable_start    Existing event with bad start time is not deleted
test_calendar_sync_preserves_out_of_window       Existing event at or after window_end is not deleted
test_calendar_sync_deletes_stale_in_window       Existing event inside [start, end) missing from crawl is deleted
test_failed_crawl_preserves_all_deadlines        deadlines=None causes 0 deletions
test_successful_empty_cleans_inside_window_only  items=[] deletes stale inside [start, end) only
```

---

## 19. Updated Decision Matrix

| Dimension | Legacy Playwright (`main`) | PR #23 Moodle API Client | Proposed Playwright DOM Crawler |
| :--- | :--- | :--- | :--- |
| **Network Reachability (Azure CI)**| ❌ Fails (TCP connect timeout) | ❌ Fails (TCP connect timeout) | ❌ Fails without Gate 0 alternate egress |
| **Execution Latency** | ❌ Slow (Measured: 75.8s in CI) | ✅ Fast (Estimated: 2–3s) | ⚠️ Bounded (Estimated: 12–18s for 5–9 pages) |
| **DOM / Theme Brittleness** | ❌ Extreme (30+ pages, regex) | ✅ Zero (JSON API) | ⚠️ Low–Medium (Month DOM + candidate tables) |
| **Identity Quality** | ❌ Weak (`activity_url` fallback) | ✅ Strong (`moodle_event:<id>`) | ✅ Strong (`moodle_event:<id>` via `data-event-id`) |
| **Completion Filtering** | ⚠️ Brittle ("Not completed" icon) | ✅ Native (`action.actionable`) | ✅ High (`td.submissionstatussubmitted` for assignments) |
| **Completeness Horizon** | ❌ Undefined | ✅ Authoritative 120 days | ✅ Proven 45–60 days (Current + Next Month) |
| **Partial Failure Safety** | ❌ **UNSAFE** (swallows errors) | ✅ **SAFE** (fails closed) | ✅ **SAFE** (fails closed) |
| **Calendar Deletion Safety** | ❌ **UNSAFE** (global deletion) | ✅ **SAFE** (strict `[start, end)`) | ✅ **SAFE** (strict `[start, end)`) |

---

## 20. Required Final Architecture Contract

```text
TARGET ALGORITHM
1. Playwright launch Chromium headless (minimal single-browser, single-context lifecycle).
2. Authenticate via form login ('form#login input[name="username"]:not([type="hidden"])').
3. Compute exact [window_start, window_end) where:
   - window_start = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(microsecond=0)
   - window_end   = 00:00:00 on the 1st day of the month following the final crawled month.
4. Navigate directly to current-month URL (/calendar/view.php?view=month&time=<current_month_ts>).
   - Verify authenticated session valid and expected month heading rendered.
   - Extract candidate event cards requiring valid numeric 'data-event-id'.
5. Navigate directly to next-month URL (/calendar/view.php?view=month&time=<next_month_ts>).
   - Verify authenticated session valid and expected month heading rendered.
   - Extract candidate event cards requiring valid numeric 'data-event-id'.
6. Filter candidate cards against event-kind policy:
   - Match open events ('bắt đầu' / 'opens') -> EXCLUDE.
   - Unsupported candidate deadline kinds (/mod/quiz/, generic completion) -> FAIL CLOSED (raise ElearningCrawlError).
   - Unrecognized pattern -> FAIL CLOSED (raise ElearningCrawlError).
7. For candidate assignment due events (/mod/assign/):
   - Navigate to candidate activity URL.
   - Check 'table.submissionstatustable td.submissionstatussubmitted'.
   - If submitted -> EXCLUDE.
   - If unsubmitted -> INCLUDE.
   - Any activity navigation timeout or missing status table -> FAIL CLOSED (raise ElearningCrawlError).
8. Filter surviving items to exact interval: window_start <= due_at < window_end.
9. Return DeadlineCrawlResult(items=surviving_items, window_start=window_start, window_end=window_end).

ORCHESTRATOR CONTRACT
- Any crawler exception -> deadlines = None, deadline_window = None.
- Complete empty crawl  -> deadlines = [], deadline_window = (start, end).
- Complete crawl with items -> deadlines = [...], deadline_window = (start, end).

CALENDAR RECONCILIATION CONTRACT
- Delete only stale deadline events whose parsed starts are strictly inside [window_start, window_end).
- Unparsable start or out-of-window = PRESERVE.
- Fail closed with ValueError if deadlines list is passed without valid aware window.

NETWORK PREREQUISITE
- Playwright does not change the egress IP. Production deployment requires Gate 0 alternate egress.

ROLLBACK CONTRACT
- If crawler is unhealthy, orchestrator sets deadlines = None -> 0 deletions, preserve all Calendar events.
```

---

## 21. Senior Review Gates

```text
Gate Identifier                          Status              Evidence / Resolution Required
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate A — Stable Identity                 PASS                Verified: data-event-id is rendered on Moodle
                                                             calendar cards (5/5 sample contained numeric ID).
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate B — Completion/Actionable Semantics PASS (v1 Scope)     Verified: Policy B actionable detection is proven
                                                             for /mod/assign/ via td.submissionstatussubmitted.
                                                             Unsupported kinds fail closed to protect calendar.
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate C — Authoritative DOM Horizon       PASS                Verified: Direct month navigation proves complete
                                                             coverage for Current Month + Next Month (45–60d)
                                                             with exact [start, first_day_next_month_00:00:00).
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate D — Partial Failure Safety Design   PASS                Fail-closed architecture: any error aborts crawl
                                                             and sets deadlines = None.
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate E — Calendar Authority Contract     PASS                Preserves PR #23's [start, end) boundary protection
                                                             and unparsable event preservation.
────────────────────────────────────────────────────────────────────────────────────────────────────────
Gate F — Production Network Reachability EXTERNAL BLOCKER    Azure GitHub Actions runners cannot connect to
                                                             elearning.tdtu.edu.vn. Requires alternate egress.
```

### Authorization Decisions:

- **Architecture Approval**: **APPROVED**
- **Implementation Approval**: **APPROVED**
- **Production Deployment Approval**: **BLOCKED BY GATE F** (awaiting alternate egress provisioning)
