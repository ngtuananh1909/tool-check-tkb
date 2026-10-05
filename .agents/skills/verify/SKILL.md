# Verify skill

Use this repo-level recipe when verifying runtime behavior changes.

## Calendar and Telegram dry-run flows

- Avoid real Google Calendar/Telegram calls for local verification.
- Use a temporary directory for generated `exports/` files.
- Patch `_build_calendar_service()` with a fake service whose `events().list().execute()` returns Google Calendar event dicts.
- Patch Telegram sending functions or `requests.post` so `main.main()` reaches `send_daily_summary()` without network.
- Drive these surfaces:
  - `main.main()` for morning summary behavior.
  - `telegram_mvp_bot` form helpers through the same state transitions used by polling/webhook handlers: `/add` date → time → job → skip location/done.
- Capture stdout showing:
  - Calendar query range.
  - `Today has ... appointment(s)` / `Upcoming exam set ...` log lines.
  - Captured `send_daily_summary()` arguments.
  - `/add` prompt, stored state, review text, and invalid-time error.
