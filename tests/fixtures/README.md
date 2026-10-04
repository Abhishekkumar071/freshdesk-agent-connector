Fake Freshdesk API response bodies used by the tests. The shapes follow the
official API reference and what a trial account returned (see
docs/freshdesk-api-notes.md). All names, emails, IDs and order numbers are invented.
Never paste real API responses here.

error_401.json is a plausible body only; no test depends on its fields, because
the connector maps a 401 by status code alone.
