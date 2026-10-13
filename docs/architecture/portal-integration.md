# Self-service portal integration

Decision record: [ADR-0009](../adr/0009-portal-integration.md).

The self-service portal is a REST API that, per study or package, returns **where the package must be delivered**
(user decision D2):
- another S3 bucket/prefix, or
- an external server (SFTP, HTTPS, more types later).

It can also return metadata and schedule information. It is modelled as a first-class `portal` section in
`spec/integrations.yaml`, with the per-environment `base_url` in `spec/environments.yaml`. The **destination
allow-list** that bounds what the portal may choose is in `spec/destinations.yaml`, owned by governance
([ADR-0024](../adr/0024-portal-resolved-destinations.md)).

Portal changes **take effect immediately** (user decision D4). There is no PR gate on portal data. The safety
comes from the allow-list, which is a reviewed spec file.

## 1. Spec

```yaml
# spec/integrations.yaml (excerpt; the full file is in examples/team-pipelines/pipelines/study_pkg_delivery)
portal:
  auth: {type: oauth2_client_credentials, token_path: /oauth2/token, client: portal_client, scopes: [delivery-targets.read]}
  reliability: {connect_timeout: 5s, read_timeout: 30s, max_attempts: 5, retry_on_status: [429, 500, 502, 503, 504],
                honour_retry_after: true, circuit_breaker: {failure_threshold: 5, reset_after: 60s}, rate_limit_per_second: 5}
  endpoints:
    - id: delivery_targets
      method: GET
      path: "/api/v1/studies/{study_id}/delivery-targets"     # tokens: declared variables only
      pagination: {type: none}                                 # none | page | cursor | link_header
      response:                                                # → generated Pydantic model, fail fast
        unknown_fields: ignore
        fields:
          study_id: {type: string}
          targets: {type: list, max_items: 10, items: {type: object, fields: {slot: {type: enum, values: [x, y]}, uri: {type: string, max_length: 1024}, active: {type: boolean}}}}
      mapping:                                                 # JMESPath only
        destinations:
          x: "targets[?slot=='x' && active] | [0].uri"
          y: "targets[?slot=='y' && active] | [0].uri"
        expect:
          - {path: study_id, equals: "{study_id}"}              # the answer must be about the study we asked for
destinations:                                                  # slots; what each may resolve to is in destinations.yaml
  - {id: x, resolve: {via: portal, endpoint: delivery_targets}, allowed_types: [https, s3, sftp], required: true}
  - {id: y, resolve: {via: portal, endpoint: delivery_targets}, allowed_types: [s3, sftp], required: true}
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

**Mapping:** JMESPath produces one destination URI per slot, plus declared values. Then:
- **Destination safety** ([ADR-0024](../adr/0024-portal-resolved-destinations.md)). Each URI is parsed strictly. It
  is rejected for:
  - a scheme other than s3/sftp/https;
  - non-ASCII characters or any `%`;
  - userinfo, a query or a fragment;
  - `.` or `..` segments;
  - an IP literal.

  The URI must then match **exactly one** allow-list entry of the current environment: same type, host or bucket,
  and port; a path matching the entry's pattern; and every `{var}` in the pattern equal to the run's own value (so
  study A can never be routed to study B's folder). Credentials, KMS keys and egress come from the matched entry,
  never from the portal. No match is a `security` error.
- **Expect guards** (e.g. `study_id` equals the requested study) reject answers about the wrong entity.

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

Portal calls need portal credentials, so they **never run on MWAA**. They run in the team account:
- In a pipeline task, the `portal.lookup` op runs, usually in the first Lambda or pod task. The snapshot is written
  once and every later task reads it.
- **Per-entry schedules** from the portal (`trigger.type: portal_schedule`, a later minor version) use a dispatcher
  Lambda on EventBridge Scheduler in the team account. It:
  1. snapshots the portal;
  2. computes the entries that are due, deterministically;
  3. triggers the worker DAG with `dag_run_id = run_key` through the cross-account trigger role
     ([ADR-0010](../adr/0010-event-triggers-and-sqs-custody.md)).

## 4. Governance

- Portal data takes effect at run time (D4). Every resolution is audited: run_id, slot, matched allow-list entry
  id, URI hash.
- The **allow-list** (`spec/destinations.yaml`) is the reviewed control. Adding a destination needs a PR approved by
  governance (CODEOWNERS), which is the Part 11 signature (D5).

## 5. Testing

- **Mock generation.** From `response_schema` (or the portal's OpenAPI, if provided) a mock is generated:
  - an `httpx.MockTransport` for unit tests;
  - a local HTTPS server (TLS, optional mTLS) for integration tests, extending `dryrun/servers.py`.
- **Synthetic data only.** Fixtures are generated from the schema, never copied from the real portal.
- **Fault scenarios:**
  - 429 with `Retry-After`;
  - 503 bursts until the breaker opens;
  - schema drift (a missing or extra field);
  - a destination outside the allow-list (other host, other bucket, other study's folder, `..`, `%2e`, IP literal);
  - a pagination loop;
  - token expiry mid-run;
  - an mTLS certificate rejected;
  - slow responses (timeout).
- **Contract test.** When the portal team publishes an OpenAPI or JSON Schema, CI checks the spec's
  `response_schema` against it.
