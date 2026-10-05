# Browser protection and machine credential rollout

## Current compatibility boundary

Browser reads, History health, temporary machine creation, and WebSocket snapshots require a valid Firebase user. Existing account authorization is unchanged: these operations require a signed-in user, not an administrator claim. Imported History and account creation retain their administrator requirement.

The real machine producers run on external computers that are currently unavailable for modification. `MACHINE_INGESTION_MODE=legacy` (also the default when unset) preserves their existing HTTP payload contract. **Legacy mode still permits unauthenticated real machine writes and does not resolve their integrity risk.** A throttled warning identifies each affected project and bounded canonical machine names seen in its requests, without logging amounts, bodies, credentials, or source addresses. This is migration evidence, not proof that a request was durably accepted or that a sender is trustworthy.

Temporary-machine metadata is rejected by `/api/v1/project2_data` before any storage or History operation. Signed-in temporary writes must use `/api/v1/project2_temporary_data` with `Authorization: Bearer <Firebase ID token>`. A machine credential cannot authorize temporary writes.

## Browser deployment order

1. Publish the compatible frontend first. Add Bearer authentication to History GETs. On each WebSocket open, send the first frame as `{"type":"authenticate","token":"<Firebase ID token>"}`; never include the token in the URL. Obtain a current token for every reconnection.
2. Verify that the frontend still works with the earlier backend. The earlier backend ignores this first frame and preserves its existing snapshot flow.
3. Publish the backend. It sends no application data until the first frame authenticates, allows 10 seconds for authentication, and limits the authentication frame to 16 KiB. Invalid credentials close with 4401, disallowed browser origins with 4403, and a verification service outage with 1013. An authenticated connection closes at token expiry and must reconnect with a refreshed token.
4. Refresh existing browser tabs and verify signed-in live snapshots and History. Unauthenticated History must return 401; unauthorized WebSockets must receive no application snapshot. Do not send synthetic machine data or Telegram messages as a deployment check.

HTTP JSON bodies have a 2 MiB streamed limit, including requests with missing or dishonest Content-Length headers. Protected-route CORS permits `https://khodalmaa.in` and `https://www.khodalmaa.in`. `EXTRA_BROWSER_ORIGINS` can contain comma-separated exact origins for explicitly authorized development environments. Wildcards, paths, embedded credentials, queries, and fragments are rejected.

The inaccessible producers may themselves be browser-based. To preserve their existing contract, **only** POST requests and preflights for `/api/v1/project1_data` and `/api/v1/project2_data` retain the previous any-origin, no-cookie-credentials CORS response while mode is `legacy`. The temporary route is excluded. Required mode or invalid credential configuration uses the exact-origin policy for these routes too. This exception is part of the unresolved legacy machine-input risk. CORS is a browser control, not producer authentication; non-browser clients are governed by the machine credential checks.

## Scoped machine credentials

When the external software becomes available, configure `MACHINE_INGESTION_CREDENTIALS_JSON` as a JSON array. Each entry has:

```json
[
  {
    "id": "project10-machine1-v1",
    "sha256": "<64 hexadecimal characters: SHA256 of the random secret>",
    "projects": ["project10"],
    "machines": ["machine1"]
  }
]
```

This is a shape example, not a valid credential. Generate a cryptographically random secret of at least 32 characters, store only its SHA256 hash on the backend, and put the secret in the corresponding producer's protected configuration. IDs contain 1–64 letters, numbers, underscores, or hyphens; scopes list exact canonical projects and lowercase machine names. Do not expose credentials in browser bundles, query strings, logs, or the repository.

The producer adds:

```text
Authorization: Machine <id>.<secret>
```

The backend compares the fixed-length digest in constant time. A valid key must allow the requested project and every submitted machine name. A supplied `Machine` credential that is invalid, revoked, or outside its scopes is denied even in legacy mode. Non-Machine Authorization headers are ignored only in legacy mode to avoid changing unknown pre-existing producer behavior; required mode rejects them.

Roll out one producer at a time while retaining `legacy`. After every producer has been updated and its ordinary live data verified, explicitly set `MACHINE_INGESTION_MODE=required`. Required mode rejects requests lacking a valid scoped key. Missing keys, malformed credential configuration, and invalid modes fail closed with a sanitized 503 response; they never fall back to legacy.

For rotation, add a second key ID with the same necessary scope, change that producer to the new ID and secret, verify its normal live traffic, then remove the old ID. Removing an ID revokes it without affecting other producers. The application does not provide a public endpoint that creates or reveals machine credentials.

Do not switch to required mode while inaccessible producers still lack credentials. That would stop their live updates and History recording.
