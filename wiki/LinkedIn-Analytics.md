# LinkedIn Analytics

LinkedIn is an optional provider behind the existing `ANALYTICS_COLLECT`
dispatcher. It does not own an event route and therefore cannot replace the
Plausible route. Select it with an event payload such as:

```json
{"provider":"linkedin", "publication_attempt_id": 42}
```

The manual `POST /api/analytics/collect` endpoint preserves this provider
field and rejects unknown providers before queueing. Without an explicit
provider, only the explicit channels `LINKEDIN`, `PUBLISH_LINKEDIN`, and
`PUBLISH_LINKEDIN_PRO` infer LinkedIn; other channels never use broad substring
matching.

The provider reuses the LinkedIn Pro Playwright storage state at
`CORE_DATA/sessions/linkedin_auth.json` (or `LINKEDIN_AUTH_PATH`). The file
must be a Playwright storage-state document with restrictive permissions. A
missing, invalid, expired, or challenge session produces `BLOCKED_AUTH` and
`AUTH_REQUIRED`; no real snapshot is written.

## Metrics and identity

Collection is read-only and currently records cumulative (`lifetime`) post
card metrics. `collected_at` distinguishes later 24-hour, 7-day, and 30-day
collection runs; these are not falsely labelled interval metrics. Views and
impressions remain separate, as do reactions and likes. Comments are counts
only. Missing values remain unavailable and measured zero remains zero.

Attribution uses the publication ledger ID and an exact publication permalink.
A platform ID/URN is retained as supporting ledger evidence, but a standalone
ID without a navigable permalink is not sufficient for the current collector.
Generic feed,
recent-activity, company-post listing, and admin listing URLs are rejected.
Text and timestamp matching is never used. Only publication-specific HTTPS
URLs on `linkedin.com` are accepted.

The LinkedIn capability must be registered and active for collection. A missing
capability row fails closed, while a system with zero active analytics providers
remains healthy. Current v1 publication metrics are cumulative lifetime
metrics; requested `24h`, `7d`, or `30d` values are cadence hints and are
stored as `window=lifetime` with the collection timestamp preserved.

`mode=SIMULATED` uses deterministic fixtures without starting Playwright. It
is stored as SIMULATED and is excluded from normal performance, dashboard and
evergreen views. Provider capability rows (`LinkedIn Analytics` and
`Plausible Analytics`) can be disabled independently; an installation with no
active provider remains valid.

The normalized result flows through `ANALYTICS_AGGREGATE`, content performance,
feedback, and the existing REAL-only evergreen consumer. No likes, comments,
follows, reposts, or publishing actions are performed.

## Troubleshooting

Install Playwright/Chromium in the configured virtual environment and create a
storage state through the normal LinkedIn session workflow. Keep session files
out of Git and never put passwords or cookies in event payloads. A challenge or
login redirect is reported as `AUTH_REQUIRED`; temporary navigation failures
are safe failures and can use the worker's retry policy.
