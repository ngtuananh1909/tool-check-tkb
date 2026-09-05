# TDTU eLearning API Implementation Plan

## 1. Executive Decision

We will replace the Playwright browser-based eLearning deadline crawler with a lightweight, pure HTTP client utilizing Moodle's authenticated AJAX endpoint `core_calendar_get_action_events_by_timesort`.

Key decisions:
- **Authentication**: Pure HTTP `requests.Session` logging into `https://elearning.tdtu.edu.vn/login/index.php` using form `logintoken` and extracting `sesskey` from `/calendar/view.php`.
- **Primary API**: `core_calendar_get_action_events_by_timesort` under `/lib/ajax/service.php?sesskey=<sesskey>`.
- **Runtime Playwright Fallback**: **NONE**. There is no runtime Playwright fallback for eLearning. If HTTP authentication or API calls fail, the system fails closed with `ElearningAuthError` / `ElearningApiError`, sets `deadlines = None`, logs sanitized diagnostics via `logger.exception`, skips deadline reconciliation, and preserves all existing Google Calendar deadline events.
- **Playwright Dependency**: **DO NOT remove Playwright from `requirements.txt`**. Playwright remains required as a fallback crawler for TDTU student portal schedule/exam tables (`old-stdportal.tdtu.edu.vn`).
- **Product Scope**: Actionable eLearning deadlines and tasks only (assignments, quizzes, activity completions with due dates).
- **Authoritative Date Window**: Defined strictly as the half-open interval `[window_start, window_end)`.
- **Actionable Filtering**: ONLY events where `action.actionable is True` are included as normalized deadlines. Missing or non-True `actionable` values are strictly filtered out (never defaulted to `True`).
- **Timezone Contract**: Timezone-aware datetimes end-to-end (`APP_TIMEZONE = Asia/Ho_Chi_Minh`). Naive datetimes are strictly forbidden.

---

## 2. Confirmed Discovery Findings

Empirical testing on the live TDTU eLearning installation confirmed:
1. **Pure HTTP Login**: `requests.Session` successfully authenticates via form POST with `logintoken`, receives `MoodleSession` cookie, and accesses authenticated pages without Playwright (`PURE HTTP LOGIN CONFIRMED`).
2. **AJAX API Execution**: `POST /lib/ajax/service.php?sesskey=<sesskey>` with `core_calendar_get_action_events_by_timesort` returns structured JSON containing actionable events.
3. **Pagination Mechanism**:
   - `limitnum` controls page size.
   - Response contains `firstid`, `lastid`, and `events`.
   - Passing `aftereventid = lastid` fetches the next page strictly after `lastid`.
   - **Empirical Test (`limitnum=2`)**: Page 1 (`lastid=392689`), Page 2 (`aftereventid=392689`) yielded **0 page boundary overlap items**. The cursor advances cleanly.
4. **Time Window Filtering**: Server-side filtering via `timesortfrom` and `timesortto` is deterministic and authoritative.
5. **Stable Identity**: Moodle event ID (`event["id"]`) is unique and stable. `source_signature = f"moodle_event:{event_id}"` maps cleanly into Google Calendar sync keys (`deadline:moodle_event:<event_id>`).

---

## 3. Git & Branch Context

- **Base branch**: `main`
- **Base commit**: `72f2be1` (latest `origin/main`)
- **Working branch**: `feature/elearning-calendar-api-v2`

---

## 4. Current Architecture

Currently, `run_hour.py` executes:
```text
run_hour.py
  └─► crawler.fetch_elearning_deadlines()
        └─► Playwright Chromium launch
        └─► POST login
        └─► GET /my/ (dashboard)
        └─► Parse courses
        └─► Visit each course page (DOM parsing)
        └─► Return list[dict]
  └─► calendar_sync.sync_crawled_data_to_google_calendar(deadlines=deadlines)
        └─► _replace_bot_events_for_range() deletes ALL existing Google deadline events not present in `deadlines`
```

---

## 5. Target Architecture

```text
run_hour.py
  └─► elearning.ElearningClient(student_id, password)
        └─► requests.Session HTTP login
        └─► GET /calendar/view.php (sesskey)
        └─► fetch_deadline_result(days_ahead=120)
              └─► POST /lib/ajax/service.php (paginated loop)
        └─► returns DeadlineCrawlResult(items=..., window_start=..., window_end=...)
  └─► calendar_sync.sync_crawled_data_to_google_calendar(
          deadlines=result.items,
          deadline_window=(result.window_start, result.window_end)
      )
        └─► _replace_bot_events_for_range() deletes ONLY obsolete deadline events falling strictly inside [window_start, window_end)
```

---

## 6. Production Data Contract & Authoritative Window Semantics

To ensure deadline data and authority boundaries travel together without risk of policy drift, the crawler returns a unified dataclass:

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class DeadlineCrawlResult:
    items: list[dict]
    window_start: datetime  # Timezone-aware datetime (Asia/Ho_Chi_Minh)
    window_end: datetime    # Timezone-aware datetime (Asia/Ho_Chi_Minh)
```

### Authoritative Window Interval Definition: `[window_start, window_end)`

The domain-level authoritative deadline window MUST be evaluated as the half-open interval:
```python
window_start <= deadline_start < window_end
```

**Why `[window_start, window_end)` is mandatory**:
1. **Prevents double ownership**: An event occurring exactly at `window_end` belongs exclusively to the subsequent window, avoiding dual processing.
2. **Deterministic execution**: Hourly sync jobs evaluating fixed time bounds will produce exact non-overlapping coverage partitions.
3. **Eliminates boundary ambiguity**: Standardizes range semantics across client, mapper, and Google Calendar reconciliation.
4. **Moodle API boundary translation**: If Moodle's API arguments (`timesortfrom`, `timesortto`) exhibit inclusive edge behavior, `ElearningClient` translates them internally so the domain contract remains strictly `[window_start, window_end)`.

---

## 7. Failure & Partial Result Semantics (`None` vs `[]`)

The pipeline contract relies on explicit return values and strict validation to protect Google Calendar data integrity:

### 1. Mandatory `deadlines` / `deadline_window` Pair Contract
- **Rule 1**: Whenever `deadlines` is passed as a list (whether empty `[]` or containing items `[...]`), `deadline_window` **MUST BE PROVIDED** as a valid `tuple[datetime, datetime]` representing `(window_start, window_end)`. If `deadlines` is a list and `deadline_window` is missing (`None`), `sync_crawled_data_to_google_calendar` **MUST FAIL CLOSED / REJECT IMMEDIATELY** (raise `ValueError("deadlines list provided without authoritative deadline_window; sync aborted to prevent global deletion")`). **NEVER perform global deletion when `deadline_window` is missing!**
- **Rule 2**: `deadline_window` **MUST NOT EXIST** if `deadlines is None`. If `deadlines is None`, `deadline_window` MUST also be `None`. Passing `deadlines=None` with a non-None `deadline_window` is invalid and raises `ValueError`.

### 2. Successful Crawl with Events
- Returns `DeadlineCrawlResult(items=[...], window_start=A, window_end=B)`.
- **Reconciliation**: Reconciles deadline events inside `[A, B)`. Upserts active deadlines and removes obsolete Google Calendar deadlines falling inside `[A, B)`.

### 3. Successful Authoritative Empty Result
- Returns `DeadlineCrawlResult(items=[], window_start=A, window_end=B)`.
- Indicates Moodle authoritatively has zero action events in `[A, B)`.
- **Reconciliation**: Removes stale Google Calendar deadlines falling inside `[A, B)`. Leaves deadline events outside `[A, B)` completely untouched.

### 4. Failed or Incomplete Crawl
- Returns `None` for crawl result (`deadlines = None`, `deadline_window = None`).
- Triggered by ANY error (invalid login, network timeout, HTTP non-200, missing `sesskey`, malformed JSON payload, pagination cursor stall, duplicate IDs across pages).
- **Reconciliation**: Google Calendar deadline reconciliation is **SKIPPED ENTIRELY**. Zero deadline events are deleted.
- **Rule**: A partial pagination crawl (e.g. page 1 succeeds, page 2 fails) MUST NEVER return partial items as success. It MUST fail closed to `None`.

---

## 8. Timezone Contract (Aware Datetimes End-to-End)

To prevent bugs caused by naive datetime comparisons or timezone offset mismatch, the pipeline enforces a strict timezone contract:

1. **Application Timezone**: Standardized on `APP_TIMEZONE` (default: `"Asia/Ho_Chi_Minh"`, UTC+7). All timezone operations resolve `ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh"))`.
2. **End-to-End Aware Datetimes**: Naive `datetime` objects are strictly forbidden across the `elearning/` package, `run_hour.py`, and `calendar_sync.py`.
3. **Moodle Timestamp Parsing**: Moodle API returns `timesort` as a Unix integer epoch. `elearning.mapper` converts this directly to an aware UTC datetime, then formats it as an ISO-8601 string with explicit offset:
   ```python
   due_dt = datetime.fromtimestamp(int(timesort), tz=timezone.utc).astimezone(app_tz)
   ```
4. **Authoritative Window Datetimes**: `window_start` and `window_end` inside `DeadlineCrawlResult` are timezone-aware datetimes pinned to `APP_TIMEZONE`.
5. **Google Calendar Event Parsing**: When reading existing Google Calendar events during reconciliation, `_parse_calendar_event_start(event)` parses `start.dateTime` (ISO string with offset) or `start.date` (all-day ISO date -> combined with `time.min` at `APP_TIMEZONE`) into a timezone-aware `datetime` in `APP_TIMEZONE`.
6. **Boundary Evaluation**: Range comparison `window_start <= event_start_dt < window_end` is evaluated directly between timezone-aware `datetime` instances in `APP_TIMEZONE`.

---

## 9. Authentication Design

Implemented in `elearning/client.py`:

```python
class ElearningClient:
    BASE_URL = "https://elearning.tdtu.edu.vn"
    LOGIN_URL = "https://elearning.tdtu.edu.vn/login/index.php"
    CALENDAR_URL = "https://elearning.tdtu.edu.vn/calendar/view.php"
    SERVICE_URL = "https://elearning.tdtu.edu.vn/lib/ajax/service.php"

    def __init__(self, student_id: str, password: str, session: requests.Session | None = None):
        self.student_id = student_id
        self.password = password
        self.session = session or requests.Session()
        self.sesskey: str | None = None

    def login(self) -> None:
        # 1. GET login page -> extract logintoken
        resp = self.session.get(self.LOGIN_URL, timeout=15)
        logintoken = self._parse_logintoken(resp.text)
        
        # 2. POST credentials
        data = {"username": self.student_id, "password": self.password, "logintoken": logintoken}
        post_resp = self.session.post(self.LOGIN_URL, data=data, timeout=15, allow_redirects=True)
        if "login" in post_resp.url.lower() or "MoodleSession" not in self.session.cookies:
            raise ElearningAuthError("eLearning HTTP login failed: invalid credentials or session reject")

        # 3. GET calendar page -> extract sesskey
        cal_resp = self.session.get(self.CALENDAR_URL, timeout=15)
        self.sesskey = self._parse_sesskey(cal_resp.text)
        if not self.sesskey:
            raise ElearningAuthError("Failed to extract sesskey from calendar page")
```

---

## 10. Exception Hierarchy

Minimal typed exception hierarchy defined in `elearning/exceptions.py`:

```python
class ElearningError(Exception):
    """Base exception for all eLearning errors."""

class ElearningAuthError(ElearningError):
    """Raised when HTTP login or sesskey extraction fails."""

class ElearningApiError(ElearningError):
    """Raised when Moodle AJAX service returns an HTTP error or error payload."""

class ElearningPaginationError(ElearningError):
    """Raised when pagination invariants are violated (missing cursor, stall, loop)."""

class ElearningResponseError(ElearningError):
    """Raised when API payload is malformed or missing required schema fields."""
```

---

## 11. Pagination Algorithm & Defensive Invariants

Implemented in `elearning/client.py`:

```python
def fetch_action_events(self, window_start: datetime, window_end: datetime, page_size: int = 50) -> list[dict]:
    if not self.sesskey:
        raise ElearningAuthError("Client is not authenticated. Call login() first.")

    url = f"{self.SERVICE_URL}?sesskey={self.sesskey}"
    cursor = None
    all_events = []
    seen_cursors = set()
    seen_event_ids = set()

    while True:
        args = {
            "timesortfrom": int(window_start.timestamp()),
            "timesortto": int(window_end.timestamp()),
            "limitnum": page_size,
            "limittononsuspendedevents": True,
        }
        if cursor is not None:
            args["aftereventid"] = cursor

        payload = [{
            "index": 0,
            "methodname": "core_calendar_get_action_events_by_timesort",
            "args": args
        }]

        try:
            resp = self.session.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            res_json = resp.json()
        except Exception as exc:
            raise ElearningApiError(f"HTTP/JSON request failed: {exc}") from exc

        if not isinstance(res_json, list) or not res_json:
            raise ElearningResponseError("Invalid API envelope: expected non-empty array")

        page_res = res_json[0]
        if page_res.get("error"):
            raise ElearningApiError(f"Moodle API returned error: {page_res.get('exception')}")

        data = page_res.get("data", {})
        events = data.get("events", [])
        if not isinstance(events, list):
            raise ElearningResponseError("Invalid API payload: 'events' is not a list")

        if not events:
            break

        # Check duplicate event IDs across pages to detect pagination anomalies
        for ev in events:
            ev_id = str(ev.get("id") or "")
            if not ev_id:
                raise ElearningResponseError("Moodle event missing required 'id'")
            if ev_id in seen_event_ids:
                raise ElearningPaginationError(f"Duplicate Moodle event ID detected across pages: {ev_id}")
            seen_event_ids.add(ev_id)

        all_events.extend(events)

        if len(events) < page_size:
            break

        next_cursor = data.get("lastid")
        if next_cursor is None:
            raise ElearningPaginationError("Full Moodle event page did not provide 'lastid'")

        if str(next_cursor) in seen_cursors or str(next_cursor) == str(cursor):
            raise ElearningPaginationError(f"Pagination cursor did not advance: {next_cursor}")

        seen_cursors.add(str(next_cursor))
        cursor = next_cursor

    return all_events
```

---

## 12. Event Mapping & Actionable Filtering Rule

Implemented in `elearning/mapper.py`:

```python
def map_moodle_event(raw_event: dict, app_tz: ZoneInfo) -> dict | None:
    event_id = str(raw_event.get("id") or "").strip()
    if not event_id:
        raise ElearningResponseError("Moodle event missing required 'id' field")

    # Strict Actionable Rule: ONLY actionable == True becomes a normalized deadline.
    # NEVER default missing or non-True actionable to True!
    action = raw_event.get("action", {}) or {}
    actionable = action.get("actionable")
    if actionable is not True:
        # Ignore non-actionable events (actionable is False, None, or missing)
        return None

    timesort = raw_event.get("timesort")
    if not timesort:
        raise ElearningResponseError(f"Moodle event {event_id} missing 'timesort'")

    due_dt = datetime.fromtimestamp(int(timesort), tz=timezone.utc).astimezone(app_tz)
    course = raw_event.get("course", {}) or {}

    return {
        "moodle_event_id": event_id,
        "course_id": str(course.get("id") or "").strip(),
        "course_name": str(course.get("fullname") or "").strip() or "Môn học",
        "activity_name": str(raw_event.get("name") or "").strip() or "Deadline",
        "due_date": due_dt.isoformat(),
        "activity_url": str(raw_event.get("url") or "").strip(),
        "completion_status": "incomplete",
        "event_kind": str(raw_event.get("eventtype") or "due").strip(),
        "source_signature": f"moodle_event:{event_id}",
    }
```

### Actionable Filtering Invariant
- If `actionable` is `False`, `None`, or omitted from `action`, `map_moodle_event` returns `None`.
- `fetch_deadline_result` collects ONLY non-None mapped dictionaries:
  ```python
  items = [mapped for ev in raw_events if (mapped := map_moodle_event(ev, app_tz)) is not None]
  ```
- No non-actionable or informational calendar entries can accidentally enter the Google Calendar pipeline.

### Stable Identity Contract
Google Calendar source key format:
```text
_deadline_source_key(deadline) -> "deadline:moodle_event:<moodle_event_id>"
```

**Why Moodle Event ID is superior to Activity URL**:
- `activity_url` is not a safe primary key because one Moodle activity may generate multiple calendar events (e.g. open date, close date, due date).
- `moodle_event_id` is globally unique and stable.
- **Update Behavior**: If an existing event's title or due date changes on Moodle while retaining the same `moodle_event_id`, `calendar_sync._sync_calendar_item` computes a hash mismatch and patches/updates the existing Google Calendar event rather than creating a duplicate.

---

## 13. Google Calendar Reconciliation Changes

Modify `calendar_sync.py`:

1. Update `sync_crawled_data_to_google_calendar` signature to accept optional `deadline_window: tuple[datetime, datetime] | None = None`.
2. Enforce `deadline_window` presence validation:
   ```python
   if deadlines is not None and deadline_window is None:
       raise ValueError("deadlines list provided without authoritative deadline_window; sync aborted to prevent global deletion")
   if deadlines is None and deadline_window is not None:
       raise ValueError("deadline_window provided while deadlines is None")
   ```
3. Parse Google Calendar event start times into timezone-aware `datetime` objects using `APP_TIMEZONE` (`Asia/Ho_Chi_Minh`).
4. Update `_replace_bot_events_for_range`:

```python
# In _replace_bot_events_for_range():
for source_key, event in existing_by_key.items():
    if source_key in current_keys:
        continue
    event_source_type = _event_source_type(event)
    if event_source_type not in managed:
        skipped_other_owner.append(source_key)
        continue

    # Window safety guard for deadlines
    if event_source_type == SYNC_SOURCE_DEADLINE:
        if deadline_window is None:
            # Extra fail-safe guard: missing window MUST NOT delete deadline events!
            logger.warning("Skipping deadline deletion because deadline_window is missing.")
            continue
        window_start, window_end = deadline_window
        event_start_dt = _parse_calendar_event_start(event)
        if event_start_dt and not (window_start <= event_start_dt < window_end):
            # Event lies OUTSIDE [window_start, window_end) -> PRESERVE IT
            logger.debug("Preserving Google deadline event outside sync window: %s", source_key)
            continue

    event_id = str(event.get("id") or "").strip()
    if event_id:
        _safe_delete_calendar_event(service, calendar_id, event_id)
        deleted_count += 1
```

---

## 14. Orchestration Boundary & Safe Logging

In `run_hour.py`:

```python
elearning_deadlines = None
deadline_window = None

try:
    with ElearningClient(student_id, password) as client:
        client.login()
        crawl_result = client.fetch_deadline_result(days_ahead=120)
        elearning_deadlines = crawl_result.items
        deadline_window = (crawl_result.window_start, crawl_result.window_end)
        logger.info("eLearning deadline crawler returned %d row(s).", len(elearning_deadlines))
except Exception:
    logger.exception("eLearning API crawl failed; skipping deadline reconciliation.")
    elearning_deadlines = None
    deadline_window = None
```

### Logging Sanitization Requirement
`logger.exception` captures stack trace for production debugging. Lower-level code in `elearning/client.py` MUST sanitize all exception messages and log strings before raising or emitting, stripping out passwords, session tokens, cookies, authorization headers, and query parameters (`sesskey`, `logintoken`).

---

## 15. Package Structure & Simplicity

Keep the package layout minimal:

```text
elearning/
    __init__.py
    client.py
    mapper.py
    exceptions.py
```

- `client.py`: Owns `requests.Session`, HTTP login, `sesskey` extraction, AJAX payload POST, and pagination loop.
- `mapper.py`: Owns Moodle JSON to normalized dict mapping and strict `actionable is True` filtering.
- `exceptions.py`: Owns typed exception hierarchy.
- `__init__.py`: Exports public interface (`ElearningClient`, `DeadlineCrawlResult`, exceptions).

---

## 16. File-by-File Change Plan

| File | Change | Why | Risk |
| ---- | ------ | --- | ---- |
| `elearning/__init__.py` | [NEW] Export public client, dataclass, exceptions | Package initialization | Low |
| `elearning/exceptions.py` | [NEW] Define `ElearningError` hierarchy | Minimal typed errors | Low |
| `elearning/mapper.py` | [NEW] Define `map_moodle_event` with strict `actionable is True` filter | Moodle JSON mapping | Low |
| `elearning/client.py` | [NEW] Implement `ElearningClient` with aware datetimes | Pure HTTP login & paginated API crawler | Medium |
| `calendar_sync.py` | [MODIFY] Accept `deadline_window`, validate presence, enforce `[start, end)` preservation | Delete safety guard & fail closed | Medium |
| `run_hour.py` | [MODIFY] Instantiate `ElearningClient`, handle errors, pass `deadline_window` to sync | Orchestration integration | Medium |
| `crawler.py` | [MODIFY] Mark Playwright `fetch_elearning_deadlines` deprecated / remove in cleanup commit | Code hygiene | Low |
| `tests/test_elearning_client.py` | [NEW] Unit tests for client, login, pagination, actionable filter, aware datetimes | Automated verification | Low |
| `tests/test_calendar_sync.py` | [MODIFY] Add tests for `deadline_window` validation & deletion protection | Verification | Low |

---

## 17. Migration vs Post-Verification Cleanup

Implementation is strictly divided into two distinct phases across separate commits:

### Phase 1: Migration (Commits A, B, C)
- Build `elearning/` package and client unit tests.
- Update `calendar_sync.py` to enforce `deadline_window` presence and `[window_start, window_end)` preservation.
- Update `run_hour.py` to use `ElearningClient`.
- Validate in production. Legacy Playwright crawler code in `crawler.py` remains intact during this phase.

### Phase 2: Legacy Cleanup (Commit D)
- Remove `fetch_elearning_deadlines` and associated eLearning DOM parsing helpers from `crawler.py` ONLY after Phase 1 is proven in production.

---

## 18. Fixed Rollback Plan & Commit Sequence

To guarantee rollback is always clean and deterministic regardless of migration stage, implementation will follow this exact 4-commit sequence:

```text
Commit A: feat(elearning): add pure HTTP Moodle API client and unit tests
Commit B: feat(calendar): add deadline window boundary protection and strict presence validation to reconciliation
Commit C: feat(orchestration): switch run_hour.py to HTTP eLearning client
Commit D: refactor(crawler): remove legacy Playwright eLearning scraper (SEPARATE CLEANUP COMMIT)
```

### Rollback Strategy

1. **Rollback BEFORE Commit D (during migration rollout)**:
   - Command: `git revert Commit_C_SHA`
   - **Result**: `run_hour.py` instantly reverts to calling `crawler.fetch_elearning_deadlines()`. The legacy Playwright code is still present and functional.

2. **Rollback AFTER Commit D (post-cleanup)**:
   - Command: `git revert Commit_D_SHA Commit_C_SHA` (or revert merge commit)
   - **Result**: Restores legacy Playwright functions in `crawler.py` and switches `run_hour.py` back in a single atomic step.

---

## 19. Expanded Test Matrix

Unit tests in `tests/test_elearning_client.py` and `tests/test_calendar_sync.py` MUST cover all 34 assertions:

### Client / Authentication
1. `test_pure_http_login_success`: Mock GET/POST -> validates login and sesskey extraction.
2. `test_login_token_missing`: Missing `logintoken` input -> raises `ElearningAuthError`.
3. `test_credentials_rejected`: Moodle redirects back to login -> raises `ElearningAuthError`.
4. `test_session_cookie_missing`: POST returns 200 without `MoodleSession` cookie -> raises `ElearningAuthError`.
5. `test_sesskey_missing`: `/calendar/view.php` missing `sesskey` -> raises `ElearningAuthError`.

### API Response Parsing & Actionable Filtering
6. `test_actionable_true_mapped`: Moodle event with `action.actionable = True` maps to normalized deadline.
7. `test_actionable_false_ignored`: Moodle event with `action.actionable = False` is omitted (`None`).
8. `test_actionable_missing_ignored`: Moodle event with missing `actionable` is omitted (never defaulted to True).
9. `test_successful_empty_events`: Response with `events=[]` returns `DeadlineCrawlResult(items=[], ...)`.
10. `test_moodle_api_error_response`: JSON response containing `"error": true` -> raises `ElearningApiError`.
11. `test_malformed_json_schema`: Missing required fields -> raises `ElearningResponseError`.

### Pagination & Invariants
12. `test_single_partial_page`: `len(events) < limitnum` -> finishes in 1 request.
13. `test_exact_full_page_then_empty`: Page 1 returns 50 items + `lastid`, Page 2 returns 0 items -> combines correctly.
14. `test_aftereventid_uses_previous_lastid`: Verify request args pass `aftereventid = previous_lastid`.
15. `test_full_page_missing_lastid`: Page returning 50 items without `lastid` -> raises `ElearningPaginationError`.
16. `test_cursor_fails_to_advance`: API returns same `lastid` on consecutive pages -> raises `ElearningPaginationError`.
17. `test_page_2_http_failure`: Page 1 200, Page 2 500 -> raises `ElearningApiError` (`deadlines = None`).
18. `test_page_2_api_error`: Page 1 200, Page 2 Moodle error -> raises `ElearningApiError` (`deadlines = None`).
19. `test_duplicate_event_ids_across_pages`: Duplicate event ID across pages -> raises `ElearningPaginationError`.
20. `test_partial_success_never_escapes`: Verify no partial list is returned if any page fails.

### Event Mapping & Identity
21. `test_moodle_event_id_to_source_signature`: `moodle_event_id` maps to `source_signature = "moodle_event:<id>"`.
22. `test_same_activity_url_different_ids`: Two events with same URL but different IDs remain distinct.
23. `test_event_kind_normalization`: `eventtype` mapped correctly.
24. `test_timezone_aware_timestamps`: Timestamps parsed as aware datetimes in `APP_TIMEZONE` (`Asia/Ho_Chi_Minh`).

### Google Calendar Reconciliation & Window Safety
25. `test_deadlines_list_without_window_raises_error`: `deadlines=[]` with `deadline_window=None` -> raises `ValueError` (no global deletion!).
26. `test_deadlines_none_with_window_raises_error`: `deadlines=None` with `deadline_window=(A, B)` -> raises `ValueError`.
27. `test_stale_deadline_inside_window_deleted`: Existing Google deadline inside `[start, end)` missing from API -> deleted.
28. `test_stale_deadline_before_start_preserved`: Existing Google deadline before `window_start` missing from API -> PRESERVED.
29. `test_stale_deadline_at_window_end_preserved`: Existing Google deadline exactly at `window_end` -> PRESERVED (`[start, end)` boundary).
30. `test_stale_deadline_at_window_start_managed`: Existing Google deadline exactly at `window_start` missing from API -> deleted.
31. `test_failed_crawl_preserves_all_deadlines`: `deadlines = None` passed -> 0 deletions.
32. `test_successful_empty_result_deletes_inside_window_only`: `items = []` passed -> deletes stale deadlines inside `[start, end)` only.
33. `test_same_moodle_event_id_title_change_updates`: Updated title for existing `moodle_event_id` -> patched, no duplicate.
34. `test_class_exam_appointment_unaffected`: Class, exam, and Telegram appointment events remain unaffected by deadline reconciliation.

---

## 20. Implementation Order Checklist

1. [ ] Create `tests/test_elearning_client.py` and write test cases 1–24 (red state).
2. [ ] Create `elearning/exceptions.py`.
3. [ ] Create `elearning/mapper.py` with strict `actionable is True` check.
4. [ ] Create `elearning/client.py` with aware datetimes and make client tests pass (green state).
5. [ ] Create `elearning/__init__.py`.
6. [ ] Create reconciliation window safety tests (cases 25–34) in `tests/test_calendar_sync.py`.
7. [ ] Modify `calendar_sync.py` to support `deadline_window` validation and `[start, end)` enforcement.
8. [ ] Run targeted tests: `pytest tests/test_elearning_client.py tests/test_calendar_sync.py`.
9. [ ] Commit A & Commit B.
10. [ ] Modify `run_hour.py` to switch to `ElearningClient`.
11. [ ] Run full test suite (`pytest`) and commit C.
12. [ ] Perform shadow / controlled live validation run.
13. [ ] Remove legacy Playwright eLearning scraper code from `crawler.py` (Commit D).
14. [ ] Run complete test suite to confirm zero regressions.

---

## 21. Definition of Done

- [ ] Normal eLearning deadline crawling uses no browser.
- [ ] Pure HTTP authentication works reliably.
- [ ] `core_calendar_get_action_events_by_timesort` pagination is implemented using tested `aftereventid`/`lastid` behavior.
- [ ] Pagination has forward-progress and cursor stall protection.
- [ ] Partial pagination can never escape as a successful crawl (`deadlines = None` on error).
- [ ] Crawl result carries authoritative window metadata (`DeadlineCrawlResult`).
- [ ] Internal authoritative-window range semantics are strictly `[window_start, window_end)`.
- [ ] `deadlines` list MUST be accompanied by `deadline_window`; missing window raises `ValueError` and NEVER global-deletes.
- [ ] `deadline_window` MUST NOT exist if `deadlines = None`.
- [ ] ONLY `action.actionable is True` becomes a normalized deadline (never defaulted to True).
- [ ] Timezone contract is timezone-aware datetimes end-to-end (`APP_TIMEZONE`).
- [ ] `deadlines = None` never reconciles/deletes deadline events.
- [ ] Successful `items = []` reconciles strictly inside `[window_start, window_end)`.
- [ ] Google deadline events outside the authoritative window are preserved.
- [ ] Moodle event ID is used as stable deadline identity (`moodle_event:<id>`).
- [ ] Same Moodle event ID with changed title/time is updated rather than duplicated.
- [ ] Class, exam, and Telegram appointment sync behavior remains unchanged.
- [ ] No credentials, passwords, sesskey, or session tokens are logged.
- [ ] Legacy eLearning Playwright code is removed only in a separate post-verification cleanup commit.
- [ ] Playwright dependency remains in `requirements.txt` for portal fallbacks.
- [ ] Rollback strategy works cleanly both before and after legacy cleanup commit.
- [ ] Full test suite (34 test cases) passes cleanly.

---

## 22. Senior Review Resolutions

| Review Item | Resolution |
| ----------- | ---------- |
| **Rollback after legacy cleanup** | Restructured implementation into a 4-commit sequence. Legacy Playwright cleanup is isolated in a separate final commit (Commit D). Rollback post-cleanup reverts Commit D + Commit C atomically. |
| **Runtime Playwright fallback contradiction** | Resolved explicitly. There is NO runtime Playwright fallback for eLearning. Auth/API failure fails closed to `deadlines = None`, preserving Google Calendar data. Playwright is retained in `requirements.txt` solely for portal schedule/exam fallbacks. |
| **Authoritative window semantics** | Explicitly defined as half-open interval `[window_start, window_end)` (`window_start <= deadline_start < window_end`). Moodle API edge quirks are translated internally by `ElearningClient`. |
| **Crawl result carries window** | Created `DeadlineCrawlResult(items, window_start, window_end)` dataclass so deadline data and authority bounds travel together, preventing drift. |
| **Pagination forward-progress safeguards** | Added checks for `next_cursor is None` on full pages, `next_cursor == cursor` stalls, `seen_cursors` tracking, and duplicate ID detection across pages. |
| **Duplicate event handling** | Duplicate Moodle event IDs across pagination pages raise `ElearningPaginationError`, failing closed to `deadlines = None`. |
| **Logging / secret sanitization** | `run_hour.py` uses `logger.exception` for tracebacks. `ElearningClient` sanitizes log output to strip passwords, tokens, cookies, headers, and query params. |
| **Migration vs cleanup commits** | Separated into Phase 1 Migration (Commits A, B, C) and Phase 2 Cleanup (Commit D). |
| **Mandatory `deadline_window` requirement** | `deadlines` list MUST be paired with `deadline_window`. Missing window raises `ValueError` / fails closed. Global deletion is strictly impossible. |
| **`deadlines = None` & window constraint** | If `deadlines is None`, `deadline_window` MUST also be `None`. Having `deadline_window` without `deadlines` raises `ValueError`. |
| **Strict `actionable is True` filtering** | ONLY Moodle events where `action.actionable is True` become normalized deadlines. Missing or non-True `actionable` values are filtered out (never defaulted to True). |
| **Timezone contract & aware datetimes** | End-to-end timezone-aware datetimes in `APP_TIMEZONE` (`Asia/Ho_Chi_Minh`). Naive datetimes are strictly forbidden. Unix epoch converted to aware UTC then `Asia/Ho_Chi_Minh`. |
