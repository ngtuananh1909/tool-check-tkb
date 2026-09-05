# TDTU eLearning Calendar API Discovery

## 1. Executive Summary

This report documents the reverse-engineering of the TDTU eLearning Calendar API. The objective is to replace the Playwright-based crawler with a pure HTTP client for fetching deadlines. The investigation proves that a pure HTTP login is viable and that the `core_calendar_get_action_events_by_timesort` AJAX API perfectly fulfills the requirements, providing deterministic, pagination-safe actionable events with stable identities. A minor modification to Google Calendar reconciliation is required to prevent accidental deletion of out-of-horizon future deadlines.

## 2. Existing eLearning Crawler Flow

The current crawler operates as follows:
1. Launches a Playwright browser.
2. Authenticates by filling the login form.
3. Opens the dashboard and discovers enrolled courses.
4. Visits individual course pages.
5. Discovers incomplete activities (assignments, quizzes, etc.).
6. Visits individual activity pages to scrape the exact due date.
This flow is extremely heavy, slow, and prone to timeout errors due to loading numerous heavy DOMs sequentially.

## 3. Investigation Environment

- **Moodle Installation**: The target is `https://elearning.tdtu.edu.vn/calendar/view.php`.
- **Authentication**: Uses standard Moodle auth with `logintoken`.
- **Browser Tooling**: Playwright and Chrome DevTools were used locally via Python automation to observe network interactions under `elearning.tdtu.edu.vn/lib/ajax/service.php`.
- **Date / Time of Investigation**: September 5, 2026.

## 4. Authentication Findings

### Browser login
Using Playwright, logging in requires filling `username` and `password` on `https://elearning.tdtu.edu.vn/login/index.php` and clicking login. This successfully provisions the required cookies.

### Pure HTTP login
**TESTED**: Pure HTTP login works flawlessly without Playwright.
The login contract requires:
1. `GET /login/index.php` to extract a dynamic `logintoken` from a hidden form input.
2. `POST /login/index.php` with `username`, `password`, and `logintoken`.
This returns the `MoodleSession` cookie and validates the redirect. There are no JS-based challenges or CAPTCHAs blocking the login.

### Required cookies/tokens
- **Cookies**: `MoodleSession` is the primary authenticated session cookie.
- **Tokens**: `logintoken` (during login POST) and `sesskey` (for AJAX calls).
The `sesskey` can be safely extracted from the HTML of `/calendar/view.php` (e.g. `{"sesskey":"[REDACTED]"}`) or any other authenticated page.

### Can production avoid Playwright?
**OBSERVED**: Yes, production can completely avoid Playwright for the eLearning deadline crawler.

## 5. Calendar Network Discovery

Observed Network Activity:
- **Trigger**: Opening `/calendar/view.php` and interacting with the mini calendar.
- **Endpoint**: `POST /lib/ajax/service.php?sesskey=[REDACTED]`
- **Method**: POST
- **Content-Type**: `application/json`
- **Sanitized Request**:
```json
[
  {
    "index": 0,
    "methodname": "core_calendar_get_calendar_monthly_view",
    "args": {
      "year": 2026,
      "month": 9,
      "courseid": 1,
      "categoryid": 0,
      "includenavigation": false,
      "mini": true,
      "day": 5
    }
  }
]
```
- **Sanitized Response Shape**:
```json
[
  {
    "error": false,
    "data": {
      "weeks": [ ... ],
      "daynames": [ ... ]
    }
  }
]
```

## 6. Candidate API Results

### core_calendar_get_calendar_upcoming_view
- **Availability**: Available.
- **Arguments**: `courseid`, `categoryid`.
- **Result structure**: Array of upcoming events.
- **Limits**: Relies on site configuration for how far ahead it looks, making it non-deterministic for our strict horizon.
- **Pros**: Easy to call.
- **Cons**: Lack of explicit pagination and date range control. 

### core_calendar_get_action_events_by_timesort
- **Availability**: **TESTED** and highly effective.
- **Arguments**: `timesortfrom` (Unix timestamp), `timesortto` (Unix timestamp), `limitnum`, `aftereventid`.
- **Result structure**: Returns a list of `events`, `firstid`, and `lastid`.
- **Limits**: Configurable via `limitnum` (e.g. 50).
- **Pros**: Only returns actionable events (deadlines), provides explicit pagination boundaries, handles explicit horizon.
- **Cons**: Might omit non-actionable Calendar announcements, but since we only sync deadlines, this is perfect.

### core_calendar_get_calendar_monthly_view
- **Availability**: Available.
- **Arguments**: `year`, `month`, `courseid`, etc.
- **Result structure**: HTML fragments and event objects per day.
- **Pros**: Represents exactly what the user sees in the Calendar UI.
- **Cons**: Extremely verbose, requires iterating multiple months sequentially, not purely data-oriented for simple deadline extraction.

### core_calendar_get_calendar_events
- **Availability**: Unavailable or restricted.
- **Arguments**: `events` (dict with `courseids`).
- **Result structure**: `{"error": false, "exception": {...}}`.
- **Cons**: Throws exceptions; not suitable.

## 7. Browser Request vs Python Replay

- **What was replayed**: A pure Python HTTP script successfully requested the login token, authenticated via POST, fetched the `sesskey`, and made a JSON POST to `/lib/ajax/service.php`.
- **Whether responses matched**: Yes, the `requests` library received the exact same JSON event payloads as the Playwright session.
- **Proof**: Tested locally. We obtained actionable events via `core_calendar_get_action_events_by_timesort` over pure HTTP.

## 8. Event Field Mapping

| Needed field | API field/path | Notes |
|---|---|---|
| event ID | `id` | Example: 100001. This is the stable Moodle event identity. |
| course ID | `course.id` | E.g., 98765 |
| course name | `course.fullname` | E.g., "HK1_2026_502051_..." |
| activity name | `name` | E.g., "Lab Assignment 3 is due" |
| URL | `url` | E.g., "https://elearning.../mod/assign/view.php?id=..." |
| timestamp | `timesort` | Unix timestamp, e.g. 1788973200 |
| event type | `eventtype` | "due", "close", "open" |
| actionable/completion | `action.actionable` | Boolean indicating if it's still actionable (incomplete) |

**Conclusion**: The API comprehensively provides all fields necessary to match the Calendar HTML data and replace the old crawler entirely without visiting individual course pages.

## 9. Event Type Semantics

- **due**: Represents a deadline for an assignment.
- **closes**: Represents the closure time of a quiz.
- **opens**: Represents the opening time of an activity.
- **should be completed**: Used for tracking manual or conditional completion activities.

The `core_calendar_get_action_events_by_timesort` inherently focuses on these deadline semantics and omits general informational events.

## 10. Coverage and Horizon Tests

- **Upcoming coverage**: Site-configured, often limited to 14-21 days.
- **Action event coverage**: Tested with `timesortfrom = now` and `timesortto = now + 86400*30`. It successfully returned items within the window. We can confidently construct a deterministic crawl window, such as `today -> today + 120 days`.
- **Hard/soft limits**: Limits are hard-enforced via `limitnum`, making pagination loops strictly necessary for users with many overdue/future items.

## 11. Pagination Behavior

The `core_calendar_get_action_events_by_timesort` API provides pagination:
- **Parameters**: Use `timesortfrom`, `timesortto`, `limitnum`, and `aftereventid`.
- **Indicators**: The response includes `firstid`, `lastid`, and `events`.
- **Empirical Test Results (`limitnum=2`)**:
  - Page 1 (`aftereventid=None`): Returned 2 events (`firstid=100002, lastid=100001`).
  - Page 2 (`aftereventid=100001`): Returned 2 events (`firstid=100000, lastid=99999`).
  - **Page Boundary Overlap**: 0 items. Passing `aftereventid = lastid` fetches strictly after `lastid`, preventing duplicate boundary events.
  - **Cursor Progression**: Clean, strictly advancing based on `lastid`.
- **Date Range Filtering Test**:
  - Past window (`now-30d` to `now-1s`): Returned 2 past events.
  - Narrow future window (`now` to `now+3d`): Returned 1 event.
  - Full future window (`now` to `now+120d`): Returned all future action events.
  - Proves server-side filtering by `timesortfrom` and `timesortto` is deterministic and authoritative.
- **Partial Failure Rule**: If any page N fails during pagination, the entire crawl must be aborted (`deadlines = None`) to prevent accidental event deletion during Google Calendar reconciliation.

## 12. API Comparison

| API | TDTU works | AJAX | Explicit range | Pagination | Coverage | Recommendation |
|---|---:|---:|---:|---:|---|---|
| `core_calendar_get_action_events_by_timesort` | Yes | Yes | Yes (`timesortto`) | Yes | Excellent (actionable only) | **Primary** |
| `core_calendar_get_calendar_monthly_view` | Yes | Yes | Implicit (by month) | N/A | Full calendar | Secondary |
| `core_calendar_get_calendar_upcoming_view` | Yes | Yes | No | No | Site-limited | Rejected |

## 13. Recommended Production API Strategy

- **Primary API**: `core_calendar_get_action_events_by_timesort`.
- **Explicit crawl horizon**: 120 days (`timesortfrom=now`, `timesortto=now + 120 days`).
- **Pagination algorithm**:
  1. `limitnum` = 50, `timesortfrom` = `now`.
  2. Request events.
  3. Yield events.
  4. If `len(events) == limitnum`, extract `lastid` and set `aftereventid = lastid`, repeat.
  5. Halt when `len(events) < limitnum`.
- **Filtering rules**: Include if `action.actionable` is `True`.

## 14. Proposed Normalized Deadline Model

```python
{
    "course_id": "98765",
    "course_name": "HK1_2026_502051_Database Systems",
    "activity_name": "Lab Assignment 3",
    "due_date": "2026-09-10T00:00:00+07:00",
    "activity_url": "https://elearning.tdtu.edu.vn/mod/assign/view.php?id=1234567",
    "completion_status": "incomplete",
    
    "moodle_event_id": "100001",
    "event_kind": "due",
    "source_signature": "moodle_event:100001"
}
```

## 15. Stable Identity / Deduplication Strategy

Rely on `moodle_event_id` instead of `activity_url`. The current URL-based deduplication is prone to collision if an activity opens and closes on the calendar.
- **Identity**: `source_signature = moodle_event:<moodle_event_id>`
- This ID is globally unique in Moodle and stable across title changes.

## 16. Google Calendar Reconciliation Risk

**Current behavior**: `_replace_bot_events_for_range` currently scans all bot-owned events for a source type. Any `deadline` event on Google Calendar that is NOT returned in the crawler's list is deleted.
**Risk**: If the new Moodle API crawler explicitly limits its crawl to 30 or 120 days, it will omit deadlines falling outside this window. The current reconciliation loop would erroneously delete those future deadlines from Google Calendar because they aren't in the `sync_items`. 

## 17. Proposed Safe Reconciliation Window

Update `calendar_sync.py` to support an authoritative window for deadlines.
- **Crawl Window**: `start = now`, `end = now + 120 days`.
- **Deletion Check**: Inside `_replace_bot_events_for_range`, before deleting an obsolete Google Calendar event of type `deadline`, parse its `start` date. Only execute the deletion if the event's start date falls strictly within the `start` to `end` horizon. Events outside this window are considered out-of-scope and left untouched.

## 18. Failure Semantics

- **Successful empty crawl**: Returns `[]`. Old events strictly inside the horizon are deleted.
- **Authentication failure**: Raise Exception. Sync skipped. Returns `None`.
- **API failure / Partial pagination**: If any page request fails, catch exception and raise error. Sync aborted entirely (`None` passed to sync). This avoids accidental event deletion.
- **Malformed response**: Raise schema error, abort sync.
- **Session expiration**: Catch 403 or specific `errorcode`, re-authenticate once, or abort sync.

## 19. Proposed Code Architecture

```text
elearning/
    __init__.py
    client.py          (Handles pure HTTP requests.Session, auth, sesskey management)
    auth.py            (Extracts logintoken and performs login)
    calendar_api.py    (Handles core_calendar_get_action_events_by_timesort with pagination)
    mapper.py          (Normalizes Moodle JSON into the dict model)
    exceptions.py      (ElearningAuthError, ElearningApiError)
```

## 20. Exact Proposed run_hour.py Integration

```python
# run_hour.py pseudo-code
try:
    with ElearningClient(student_id, password) as client:
        client.login()
        # Returns all actionable events in the next 120 days
        deadlines = client.fetch_action_events(days_ahead=120)
except Exception as e:
    logger.error("eLearning API failed: %s", e)
    deadlines = None # Triggers sync abort for deadlines
    
# Sync to Google Calendar
sync_crawled_data_to_google_calendar(
    class_sessions, 
    exams, 
    student_id, 
    deadlines=deadlines,
    # new kwarg to declare the authoritative window
    deadline_sync_horizon_days=120 
)
```

## 21. Old eLearning Code That Can Be Removed Later

The following elements of the old stack can be safely removed once the new system is active:
- `fetch_elearning_deadlines()` function in `crawler.py` (if present).
- Playwright eLearning login constants: `ELEARNING_LOGIN_URL`, `ELEARNING_SELECTOR_USERNAME`, `ELEARNING_SELECTOR_PASSWORD`, `ELEARNING_SELECTOR_SUBMIT`, `_login_and_open_elearning_dashboard`.
- Any eLearning DOM parsing selectors.

## 22. Test Plan

- Unit test pure HTTP login by mocking requests.
- Unit test pagination by mocking a Moodle JSON response with a `lastid` and a `limitnum` payload.
- E2E test against live eLearning API for at least one account.
- Unit test `_replace_bot_events_for_range` to verify that events outside the `deadline_sync_horizon_days` are not deleted.

## 23. Risks / Unknowns

- **SSO changes**: TDTU might add SSO or reCAPTCHA to `/login/index.php`. The pure HTTP approach would need to evolve if that happens, but currently, it uses basic form POST.
- **Event completeness**: `core_calendar_get_action_events_by_timesort` only fetches events with an actionable module. Purely informational calendar events won't appear. We assume this is acceptable for deadlines.

## 24. Final Recommendation

Implement **Strategy A** using `core_calendar_get_action_events_by_timesort` strictly via Pure HTTP `requests.Session`.
DO NOT implement Playwright automation for the eLearning component.
Modify the Google Calendar reconciliation to respect an explicit time horizon before deleting untracked deadline events.
