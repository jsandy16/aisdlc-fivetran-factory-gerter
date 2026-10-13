"""security.yaml: owned by governance / QA."""

from __future__ import annotations

from typing import Literal

from ..common import StrictModel, ba
from ..fieldtypes import AwsRegion, RetentionPeriod, SortedDigests

Classification = Literal["public", "internal", "confidential", "restricted"]
CLASSIFICATION_ORDER: dict[str, int] = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
AuditEvent = Literal["delivery", "destination_resolution", "portal_call", "rule_result", "secret_access"]


class EncryptionAtRest(StrictModel):
    staging: Literal["sse_kms"] = ba("Staging", "Encryption of staged artifacts", default="sse_kms")
    s3_destinations: Literal["sse_kms", "sse_s3"] = ba(
        "S3 destinations", "Encryption of delivered objects", default="sse_kms"
    )


class Checksums(StrictModel):
    manifest: SortedDigests = ba(
        "Manifest checksums", "Per-file checksums in the manifest", default_factory=lambda: ["sha256"]
    )
    delivery_sidecars: SortedDigests = ba(
        "Delivered checksum files", "Checksum files delivered next to the package", default_factory=list
    )


class Audit(StrictModel):
    enabled: bool = ba("Audit trail", "Write audit events to the write-once audit store", default=True)
    events: list[AuditEvent] = ba("Audited events", "Which events are recorded", default_factory=list)
    evidence_retention: RetentionPeriod = ba("Evidence retention", "How long evidence is kept, e.g. 10y", default="10y")


class Retention(StrictModel):
    staging_after_success: RetentionPeriod = ba("Staging after success", "", default="7d")
    staging_after_failure: RetentionPeriod = ba("Staging after failure", "", default="30d")
    run_logs: RetentionPeriod = ba("Run logs", "", default="400d")


class Residency(StrictModel):
    allowed_regions: list[AwsRegion] = ba("Allowed regions", "AWS regions data may be processed in", min_length=1)


class SecurityAndAccess(StrictModel):
    gxp_tier: Literal["non_gxp", "gxp_indirect", "gxp_direct"] = ba("GxP tier", "Decided with QA")
    data_classification: Classification = ba("Data classification", "Highest classification of the data moved")
    encryption_at_rest: EncryptionAtRest = ba("Encryption at rest", "", default_factory=EncryptionAtRest)
    package_encryption: Literal["none"] = ba(
        "Package encryption",
        "Application-level encryption: done outside the pipeline for now (ADR-0012)",
        default="none",
    )
    checksums: Checksums = ba("Checksums", "", default_factory=Checksums)
    audit: Audit = ba("Audit", "", default_factory=Audit)
    retention: Retention = ba("Retention", "", default_factory=Retention)
    residency: Residency = ba("Residency", "Where data may be processed")


class SecurityFile(StrictModel):
    """security.yaml: owned by governance / QA."""

    security_and_access: SecurityAndAccess = ba("Security and access", "Governance decisions")
