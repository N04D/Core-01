# LinkedIn Analytics

LinkedIn is an optional provider behind the existing `ANALYTICS_COLLECT`
dispatcher. It does not own an event route and therefore cannot replace the
Plausible route. Select it with an event payload such as:

```json
{"provider":"linkedin", "publication_attempt_id": 42}
```

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

Attribution uses explicit publication ID, platform ID/URN, exact LinkedIn URL,
or the deterministic `publication:<id>` identity. Text and timestamp matching
is never used. Only `linkedin.com` HTTPS URLs are accepted.

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
