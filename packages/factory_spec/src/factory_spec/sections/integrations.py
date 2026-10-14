"""integrations.yaml: sources, the self-service portal, other APIs, destination slots and secret references."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from ..common import StrictModel, ba
from ..fieldtypes import Duration, Identifier, JmesPath, SortedStrings, TableName, TemplateStr
from .fields import RecordSpec

# ---------------------------------------------------------------- sources


class TriggerLocation(StrictModel):
    via: Literal["trigger"]
    bucket_field: Identifier = ba("Bucket field", "Trigger field holding the bucket name")
    prefix_field: Identifier = ba("Prefix field", "Trigger field holding the prefix (folder)")


class StaticLocation(StrictModel):
    via: Literal["static"]
    prefix: str = ba(
        "Prefix",
        "Folder under the bound bucket, ending with / (empty: the whole bucket)",
        default="",
        max_length=900,
        pattern=r"^(([A-Za-z0-9_][A-Za-z0-9._-]*/)+)?$",
    )


class S3Source(StrictModel):
    id: Identifier = ba("Source id", "Stable id")
    type: Literal["s3"]
    label: str = ba("Label", "What this source is", default="", max_length=200)
    location: Annotated[TriggerLocation | StaticLocation, Field(discriminator="via")] = ba(
        "Location", "Where the bucket and prefix come from"
    )


class DatabaseSource(StrictModel):
    id: Identifier = ba("Source id", "Stable id (also the Fivetran connection name)")
    type: Literal["database"]
    label: str = ba("Label", "What this source is", default="", max_length=200)
    engine: Literal["postgres", "mysql", "sqlserver", "oracle"] = ba("Database engine", "Database product")
    credentials: Identifier = ba("Credentials", "Secret name holding the connection credentials")
    tables: list[TableName] = ba("Tables", "schema.table names to replicate", min_length=1)


Source = Annotated[S3Source | DatabaseSource, Field(discriminator="type")]

# ---------------------------------------------------------------- portal and APIs


class OAuth2ClientCredentials(StrictModel):
    type: Literal["oauth2_client_credentials"]
    token_path: str = ba("Token path", "Path of the token endpoint on the API host", pattern=r"^/[A-Za-z0-9._/-]*$")
    client: Identifier = ba("Client secret", "Secret name holding client_id and client_secret")
    scopes: SortedStrings = ba("Scopes", "OAuth scopes", default_factory=list)


class ApiKeyAuth(StrictModel):
    type: Literal["api_key"]
    secret: Identifier = ba("Key secret", "Secret name holding the API key")
    send_in: Literal["header", "query"] = ba("Sent in", "Header or query parameter", default="header")
    name: str = ba("Header/parameter name", "e.g. X-Api-Key", pattern=r"^[A-Za-z0-9_-]{1,64}$")


class MtlsAuth(StrictModel):
    type: Literal["mtls"]
    cert: Identifier = ba("Client certificate", "Secret name holding the client certificate")
    key: Identifier = ba("Client key", "Secret name holding the client key")
    ca: Identifier | None = ba("CA bundle", "Secret name holding a private CA bundle", default=None)


ApiAuth = Annotated[OAuth2ClientCredentials | ApiKeyAuth | MtlsAuth, Field(discriminator="type")]


class NoPagination(StrictModel):
    type: Literal["none"]


class PagePagination(StrictModel):
    type: Literal["page"]
    page_param: str = ba("Page parameter", "Query parameter carrying the page number", default="page")
    size_param: str = ba("Size parameter", "Query parameter carrying the page size", default="size")
    size: int = ba("Page size", "Items per page", default=100, ge=1, le=10_000)


class CursorPagination(StrictModel):
    type: Literal["cursor"]
    cursor_param: str = ba("Cursor parameter", "Query parameter carrying the cursor", default="cursor")
    next: JmesPath = ba("Next cursor", "Where the next cursor is in the response")


class LinkHeaderPagination(StrictModel):
    type: Literal["link_header"]


Pagination = Annotated[
    NoPagination | PagePagination | CursorPagination | LinkHeaderPagination, Field(discriminator="type")
]


class Expectation(StrictModel):
    path: JmesPath = ba("Response path", "Where to look in the response")
    equals: TemplateStr = ba("Must equal", "Expected value, e.g. '{study_id}'")


class ResponseMapping(StrictModel):
    destinations: dict[Identifier, JmesPath] = ba(
        "Destinations", "JMESPath per destination slot giving the destination URI", default_factory=dict
    )
    values: dict[Identifier, JmesPath] = ba("Values", "Named values taken from the response", default_factory=dict)
    expect: list[Expectation] = ba("Expectations", "Checks the response must pass", default_factory=list)


class Endpoint(StrictModel):
    id: Identifier = ba("Endpoint id", "Stable id")
    method: Literal["GET", "POST"] = ba("Method", "HTTP method", default="GET")
    path: TemplateStr = ba("Path", "Path template, e.g. /api/v1/studies/{study_id}/delivery-targets")
    query: dict[str, TemplateStr] = ba("Query parameters", "Query parameter templates", default_factory=dict)
    pagination: Pagination = ba("Pagination", "How results are paged")
    response: RecordSpec = ba("Response fields", "The fields the response must contain")
    mapping: ResponseMapping = ba("Mapping", "What to take from the response", default_factory=ResponseMapping)

    @model_validator(mode="after")
    def _path(self) -> Endpoint:
        if not self.path.startswith("/"):
            raise ValueError("the endpoint path must start with '/'")
        return self


class ApiReliability(StrictModel):
    connect_timeout: Duration = ba("Connect timeout", "", default="5s")
    read_timeout: Duration = ba("Read timeout", "", default="30s")
    max_attempts: int = ba("Attempts", "Total attempts on 429/5xx", default=5, ge=1, le=10)
    backoff_base: Duration = ba("First wait", "", default="1s")
    backoff_max: Duration = ba("Longest wait", "", default="30s")
    retry_on_status: list[int] = ba(
        "Retry on status", "HTTP status codes that are retried", default_factory=lambda: [429, 500, 502, 503, 504]
    )
    honour_retry_after: bool = ba("Honour Retry-After", "", default=True)
    breaker_failure_threshold: int = ba(
        "Circuit breaker threshold", "Consecutive failures that open the breaker", default=5, ge=1
    )
    breaker_reset_after: Duration = ba("Circuit breaker reset", "", default="60s")
    rate_limit_per_second: int = ba("Rate limit", "Requests per second per process", default=5, ge=1, le=1000)


class ApiSpec(StrictModel):
    label: str = ba("Label", "What this API is", default="", max_length=200)
    auth: ApiAuth = ba("Authentication", "How the factory authenticates")
    reliability: ApiReliability = ba(
        "Reliability", "Timeouts, retries, breaker, rate limit", default_factory=ApiReliability
    )
    endpoints: list[Endpoint] = ba("Endpoints", "Endpoints the pipeline calls", min_length=1)


class NamedApi(ApiSpec):
    id: Identifier = ba("API id", "Stable id")


# ---------------------------------------------------------------- destination slots

DestinationType = Literal["s3", "sftp", "https"]
DestinationTypes = Annotated[list[DestinationType], AfterValidator(lambda v: sorted(set(v)))]


class PortalResolve(StrictModel):
    via: Literal["portal"]
    endpoint: Identifier = ba("Portal endpoint", "Endpoint whose mapping gives this slot's URI")


class StaticResolve(StrictModel):
    via: Literal["static"]
    entry: Identifier = ba("Allow-list entry", "destinations.yaml entry id (same id in every environment)")
    path: TemplateStr = ba("Path", "Path or prefix under the entry, e.g. studies/{study_id}/in/")


class DestinationSlot(StrictModel):
    id: Identifier = ba("Slot id", "Stable id, e.g. x")
    label: str = ba("Label", "What this destination is", default="", max_length=200)
    resolve: Annotated[PortalResolve | StaticResolve, Field(discriminator="via")] = ba(
        "Resolution", "Where the destination comes from"
    )
    allowed_types: DestinationTypes = ba("Allowed types", "Destination types this slot accepts", min_length=1)
    required: bool = ba("Required", "The run fails if this destination cannot be delivered", default=True)


# ---------------------------------------------------------------- secrets


class SecretRef(StrictModel):
    """A reference to a secret; the value is never in a spec."""

    provider: Literal["vault", "aws_secrets_manager"] | None = ba(
        "Secret store",
        "Fallback store; each environment's binding (environments.yaml → secrets) decides where it is read from",
        default=None,
    )
    path: TemplateStr = ba("Path", "Path template; tokens: env, team, pipeline_id")
    key: str | None = ba(
        "Key", "Field inside the secret (JSON key or KV field)", default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$"
    )
    version: str | None = ba(
        "Version", "Pinned version or stage; empty = current", default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$"
    )
    kind: Literal[
        "password",
        "ssh_private_key",
        "tls_client_cert",
        "tls_client_key",
        "ca_bundle",
        "oauth_client",
        "api_key",
        "aes_key",
        "generic",
    ] = ba("Kind", "What the secret is")


class IntegrationsFile(StrictModel):
    """integrations.yaml: owned by the data engineer."""

    sources: list[Source] = ba("Sources", "Where data is read from", default_factory=list)
    portal: ApiSpec | None = ba("Self-service portal", "Portal integration", default=None)
    apis: list[NamedApi] = ba("Other APIs", "External REST APIs", default_factory=list)
    destinations: list[DestinationSlot] = ba("Destination slots", "Where data is delivered", default_factory=list)
    secrets: dict[Identifier, SecretRef] = ba("Secrets", "Named secret references", default_factory=dict)
