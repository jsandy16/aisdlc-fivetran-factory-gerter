# Self-service portal integration

Decision record: [ADR-0009](../adr/0009-portal-integration.md).

The self-service portal is a REST API that returns, per entry (a study or a delivery), the **server path**, its path
type (`absolute` or `recursive`), include/exclude patterns, metadata such as table names, and the **schedule
frequency** for that entry. It is modelled as a first-class `portal` section in `spec/integrations.yaml`, with
per-environment `base_url` in `spec/environments.yaml`.

## 1. Spec

```yaml
portal:
  auth:                                  # union, all credentials via secret names
    type: oauth2_client_credentials      # | api_key | mtls
    token_url_path: /oauth/token
    client: portal_client                # secrets.portal_client (client_id + client_secret)
    scopes: [paths.read]
    # api_key: {secret: portal_api_key, in: header, name: X-Api-Key}
    # mtls:    {cert: portal_cert, key: portal_key, ca: portal_ca}
  endpoints:
    - id: study_paths
      method: GET
      path: /api/v1/studies/{study_id}/paths      # tokens: an allow-list from trigger/conf/portal snapshot
      query: {status: active}
      pagination: {type: cursor, cursor_param: cursor, next: "meta.next_cursor"}   # none | page | cursor | link_header
      response_schema:                            # → generated Pydantic model; fail fast with a clear error
        root: object
        items: "data"
        fields:
          entry_id: {type: string, required: true}
          server_path: {type: string, required: true}
          path_type: {type: "enum[absolute,recursive]", required: true}
          include: {type: "list[string]", default: ["**/*"]}
          exclude: {type: "list[string]", default: []}
          schedule: {type: string, required: false}   # e.g. cron or "every 6h"
      response_mapping:                           # JMESPath only
        items: "data"
        entry_id: "entry_id"
        path: "server_path"
        path_type: "path_type"
        include: "include"
        exclude: "exclude"
        schedule: "schedule"
        metadata: {study_id: "study.id", source_system: "source"}
  reliability:
    connect_timeout_s: 5
    read_timeout_s: 30
    retries: {max_attempts: 5, base_s: 1, max_s: 30, on: [429, 500, 502, 503, 504], honour_retry_after: true}
    circuit_breaker: {failure_threshold: 5, reset_after_s: 60}
    rate_limit: {per_second: 5}
  snapshot: per_run                       # write-once snapshot; every task in a run uses it
  allowed_roots: ["/exports/studies/"]    # portal paths must sit under one of these
  mode: dynamic                           # | materialized (see §4)
```

## 2. Client behaviour (`factory_runtime.portal`, httpx)

**Auth:**
- `oauth2_client_credentials`: the token is cached until shortly before `expires_in`, and refreshed once on a 401.
- `api_key`: sent as a header or a query parameter.
- `mtls`: client certificate and key from `SecretRef`s, written to tmpfs (0600) for the SSL context and deleted
  after the session.
- TLS is always verified (system CA or a pinned CA bundle).

**Templating:** path and query tokens come from a fixed allow-list (`study_id`, `pipeline_id`, `entry_id`, `env`,
`run_date`, and declared metadata). Values are URL-encoded. There is no runtime Jinja.

**Pagination:**
- `page`: page/size parameters, stopping at an empty page or a total.
- `cursor`: a JMESPath to the next cursor.
- `link_header`: RFC 8288 `rel="next"`.
- A hard page cap (default 1,000) prevents loops; it is a `config` error when hit.

**Validation:** each page is checked against the generated model. Any mismatch is a `config` error that names the
field, without echoing values.

**Mapping:** JMESPath produces typed `PortalEntry` objects. Then:
- **Path safety.** Paths are canonicalised (POSIX, no `..`, no NUL) and must start with an `allowed_roots` prefix,
  otherwise it is a `security` error. The **host always comes from the spec binding**, never from the portal, which
  prevents SSRF and data exfiltration.
- **Path type.** `recursive` requires include patterns (the validator default is `**/*`, made explicit in the lock).

**Reliability:**
- Full-jitter exponential backoff on 429/5xx, honouring `Retry-After`, with an overall deadline.
- The circuit breaker and rate limiter are **per process**.
- Cross-pipeline protection comes from an Airflow pool `portal_<name>` and one snapshot per run or dispatch cycle.
  Distributed DynamoDB rate limiting was rejected because of hot keys.

**Snapshot:** the canonical JSON of the mapped entries (not the raw payload) is written to
`staging/<team>/<pipeline>/<run_key>/portal_snapshot.json` with `If-None-Match: *`. Its sha256 goes into run-state
and the manifest. Retries and later tasks read the snapshot only.

**Audit:** one event per call:
`{run_id, endpoint_id, method, path_template, status, latency_ms, attempt, page, bytes, response_sha256}`.
The payload is never logged when classification ≥ `confidential`.

## 3. Where portal calls run

Portal calls need portal credentials, so they **never run on MWAA**.

| Case | Runs in |
|---|---|
| Pipeline triggered by an event or schedule, needing the path | `portal.lookup` op in the first pod or Lambda task |
| Per-entry schedules from the portal (`trigger.type: portal_schedule`) | a **dispatcher Lambda** on EventBridge Scheduler (e.g. every 15 min) |

**The dispatcher:**
1. Snapshots the portal and hashes it.
2. Computes the entries that are due, deterministically, from each entry's schedule and the last-dispatched slot in
   run-state.
3. Triggers the worker DAG for each due entry through MWAA `InvokeRestApi` with `dag_run_id = run_key` (entry id +
   slot) and `conf = {entry_id, snapshot_sha}`. A 409 response means "already triggered".

The worker resolves the actual path from the snapshot, so paths never appear in the Airflow UI.

## 4. Governance mode

| Mode | Effect | Use |
|---|---|---|
| `dynamic` | portal changes (new entries, paths, schedules) take effect at run time | non-GxP and `gxp_indirect` (default) |
| `materialized` | a scheduled drift job compares the portal with the committed entry list and opens a PR; only merged entries run | recommended for `gxp_direct` (open question Q3) |

## 5. Testing

- **Mock generation.** From `response_schema` (or the portal's OpenAPI, if provided) a mock is generated:
  - an `httpx.MockTransport` for unit tests;
  - a local HTTPS server (TLS, optional mTLS) for integration tests, extending `dryrun/servers.py`.
- **Synthetic data only.** Fixtures are generated from the schema, never copied from the real portal.
- **Fault scenarios:**
  - 429 with `Retry-After`;
  - 503 bursts until the breaker opens;
  - schema drift (a missing or extra field);
  - a path outside `allowed_roots`;
  - a pagination loop;
  - token expiry mid-run;
  - an mTLS certificate rejected;
  - slow responses (timeout).
- **Contract test.** When the portal team publishes an OpenAPI or JSON Schema, CI checks the spec's
  `response_schema` against it.
