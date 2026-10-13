# Generates src/factory_spec/catalog/op_catalog.v1.json (the committed JSON is the source of truth at run time).
# Usage: python tools/gen_op_catalog.py src/factory_spec/catalog/op_catalog.v1.json
import json, sys
OUT = sys.argv[1]
L, P, M = "lambda", "eks_pod", "mwaa"
def port(p, kind, required=True, streaming=False, md="local"):
    return {"port": p, "kind": kind, "required": required, "streaming": streaming, "min_durability": md}
def res(lam=None, pod=None, oh=5):
    return {L: {"bytes_per_s": lam, "overhead_s": oh}, P: {"bytes_per_s": pod, "overhead_s": oh + 25}}
ops = [
 dict(op="sqs.read", version="1.0.0", status="available", categories=["integration"], summary="Read the message that triggered the run (custody snapshot in dispatch mode; receive, snapshot, dedupe, delete in in_task mode).",
      outputs=[port("message","message",md="staged")], allowed_engines=[L,P], side_effects=["reads_staging","writes_staging"],
      aws_actions=[{"actions":["s3:GetObject","s3:PutObject"],"resource":"{staging.run_prefix_arn}"},{"actions":["dynamodb:PutItem","dynamodb:GetItem"],"resource":"{run_state.table_arn}"}],
      determinism="deterministic_effect", idempotency="dedupe_key", resources=res()),
 dict(op="portal.lookup", version="1.0.0", status="available", categories=["integration"], summary="Call a portal endpoint, validate the response, map it with JMESPath and snapshot it write-once.",
      inputs=[port("request","message",required=False)], outputs=[port("answer","portal_snapshot",md="staged")], allowed_engines=[L,P],
      side_effects=["calls_portal","reads_secret","writes_staging"],
      aws_actions=[{"actions":["secretsmanager:GetSecretValue"],"resource":"{portal.secret_arns}"},{"actions":["s3:PutObject"],"resource":"{staging.run_prefix_arn}"}],
      egress=[{"host":"{portal.host}","port":"{portal.port}"}], determinism="deterministic_effect", idempotency="conditional_write", resources=res(oh=10)),
 dict(op="destination.resolve", version="1.0.0", status="available", categories=["integration"], summary="Match each destination slot's URI against the environment's allow-list (ADR-0024).",
      inputs=[port("answer","portal_snapshot"), port("request","message",required=False)], outputs=[port("destinations","destination_set",md="staged")],
      allowed_engines=[L,P], side_effects=["reads_staging","writes_staging"],
      aws_actions=[{"actions":["s3:GetObject","s3:PutObject"],"resource":"{staging.run_prefix_arn}"}], determinism="pure", idempotency="conditional_write", resources=res()),
 dict(op="fivetran.sync", version="1.0.0", status="available", categories=["integration"], summary="Trigger a Fivetran connector sync and wait for it (REST API).",
      outputs=[port("report","report")], allowed_engines=[M, "fivetran_native", "fivetran_sdk"], side_effects=["triggers_external_sync"],
      egress=[{"host":"api.fivetran.com","port":"443"}], determinism="nondeterministic", idempotency="natural", resources={}),
 dict(op="s3.list", version="1.0.0", status="available", categories=["extract"], summary="List objects under the source bucket/prefix (recursive, include/exclude, limits).",
      inputs=[port("request","message",required=False)], outputs=[port("listing","object_listing",md="staged")], allowed_engines=[L,P],
      side_effects=["reads_source","writes_staging"],
      aws_actions=[{"actions":["s3:ListBucket"],"resource":"{source.bucket_arns}"},{"actions":["s3:PutObject"],"resource":"{staging.run_prefix_arn}"}],
      determinism="deterministic_effect", idempotency="natural", resources=res(oh=10)),
 dict(op="s3.download", version="1.0.0", status="available", categories=["extract"], summary="Read the listed objects as streams, verifying size and ETag.",
      inputs=[port("listing","object_listing")], outputs=[port("files","file_set",streaming=True)], allowed_engines=[L,P], side_effects=["reads_source"],
      aws_actions=[{"actions":["s3:GetObject","s3:GetObjectVersion"],"resource":"{source.object_arns}"},{"actions":["kms:Decrypt"],"resource":"{source.kms_arns}"}],
      determinism="pure", idempotency="natural", resources=res(90_000_000, 180_000_000)),
 dict(op="rest.fetch", version="1.0.0", status="available", categories=["extract"], summary="Fetch pages from a declared HTTPS API into files.",
      inputs=[port("request","message",required=False)], outputs=[port("files","file_set",streaming=True)], allowed_engines=[L,P],
      side_effects=["calls_api","reads_secret"], aws_actions=[{"actions":["secretsmanager:GetSecretValue"],"resource":"{api.secret_arns}"}],
      egress=[{"host":"{api.host}","port":"{api.port}"}], determinism="deterministic_effect", idempotency="conditional_write", resources=res(20_000_000, 40_000_000, 10)),
 dict(op="package.zip", version="1.0.0", status="available", categories=["package"], summary="Deterministic zip preserving folder structure (streaming, zip64).",
      inputs=[port("files","file_set",streaming=True)], outputs=[port("package","package",streaming=True,md="staged")], allowed_engines=[L,P],
      side_effects=["writes_staging"], aws_actions=[{"actions":["s3:PutObject","s3:AbortMultipartUpload"],"resource":"{staging.run_prefix_arn}"},{"actions":["kms:GenerateDataKey","kms:Decrypt"],"resource":"{staging.kms_arn}"}],
      determinism="pure", idempotency="conditional_write", resources=res(60_000_000, 120_000_000)),
 dict(op="manifest.build", version="1.0.0", status="available", categories=["package","validate"], summary="Manifest of every file (path, size, sha256) and the package checksums.",
      inputs=[port("package","package"), port("listing","object_listing",required=False)], outputs=[port("manifest","manifest",md="staged")], allowed_engines=[L,P],
      side_effects=["writes_staging"], aws_actions=[{"actions":["s3:PutObject"],"resource":"{staging.run_prefix_arn}"}], determinism="pure", idempotency="conditional_write", resources=res()),
 dict(op="checksum.compute", version="1.0.0", status="available", categories=["checksum"], summary="Re-read the stored object and compute digests; compare with the manifest; write sidecars.",
      inputs=[port("package","package"), port("manifest","manifest",required=False)], outputs=[port("checksum","checksum",md="staged")], allowed_engines=[L,P],
      side_effects=["reads_staging","writes_staging"], aws_actions=[{"actions":["s3:GetObject","s3:PutObject"],"resource":"{staging.run_prefix_arn}"}],
      determinism="pure", idempotency="conditional_write", resources=res(90_000_000, 180_000_000)),
 dict(op="validate.file_count", version="1.0.0", status="available", categories=["validate"], summary="The number of files is within bounds.",
      inputs=[port("listing","object_listing")], allowed_engines=[L,P], determinism="pure", resources=res()),
 dict(op="validate.total_size", version="1.0.0", status="available", categories=["validate"], summary="The total size is within bounds.",
      inputs=[port("listing","object_listing")], allowed_engines=[L,P], determinism="pure", resources=res()),
 dict(op="validate.manifest_match", version="1.0.0", status="available", categories=["validate"], summary="The manifest lists exactly the listed objects with matching sizes.",
      inputs=[port("manifest","manifest"), port("listing","object_listing")], allowed_engines=[L,P], determinism="pure", resources=res()),
 dict(op="naming.render", version="1.0.0", status="available", categories=["naming"], summary="Render a name from a restricted token template ({seq} allocated transactionally).",
      outputs=[port("name","value")], allowed_engines=[L,P], side_effects=["allocates_sequence"],
      aws_actions=[{"actions":["dynamodb:UpdateItem","dynamodb:PutItem"],"resource":"{run_state.table_arn}"}], determinism="deterministic_effect", idempotency="dedupe_key", resources=res()),
 dict(op="destination.deliver", version="1.0.0", status="available", categories=["upload"], summary="Deliver the package (+ manifest, sidecars) to a resolved destination slot, write-once, verified.",
      inputs=[port("package","package"), port("manifest","manifest",required=False), port("checksum","checksum",required=False), port("name","value"), port("destinations","destination_set")],
      outputs=[port("receipt","receipt",md="final")], allowed_engines=[L,P], side_effects=["reads_staging","writes_target","reads_secret"],
      aws_actions=[{"actions":["s3:PutObject","s3:AbortMultipartUpload","s3:GetObject"],"resource":"{entry.object_arns}"},{"actions":["kms:GenerateDataKey","kms:Decrypt"],"resource":"{entry.kms_arn}"},{"actions":["secretsmanager:GetSecretValue"],"resource":"{entry.secret_arns}"}],
      egress=[{"host":"{entry.host}","port":"{entry.port}"}], determinism="deterministic_effect", idempotency="conditional_write",
      resources={L:{"bytes_per_s":25_000_000,"overhead_s":10}, P:{"bytes_per_s":50_000_000,"overhead_s":35}}),
]
for name, cats, summary in [("s3.put",["upload"],"Upload an artifact to a static S3 destination slot (write-once)."),
                            ("s3.copy",["upload","housekeeping"],"Server-side copy of the listed objects to a static S3 destination slot (write-once)."),
                            ("sftp.put",["upload"],"Upload to a static SFTP destination slot (pinned host key, no-clobber rename)."),
                            ("https.put",["upload"],"Upload to a static HTTPS destination slot.")]:
    ext = name in ("sftp.put", "https.put")
    lam_bps = None if name == "s3.copy" else (90_000_000 if name == "s3.put" else 25_000_000)
    ops.append(dict(op=name, version="1.0.0", status="available", categories=cats, summary=summary,
        inputs=([port("objects","object_listing")] if name == "s3.copy" else [port("artifact","package"), port("name","value",required=False)]), outputs=[port("receipt","receipt",md="final")], allowed_engines=[L,P],
        side_effects=["reads_staging","writes_target"] + (["reads_secret"] if ext else []),
        aws_actions=([{"actions":["secretsmanager:GetSecretValue"],"resource":"{entry.secret_arns}"}] if ext else [{"actions":["s3:PutObject","s3:GetObject"],"resource":"{entry.object_arns}"}] + ([{"actions":["s3:GetObject","s3:GetObjectVersion"],"resource":"{source.object_arns}"}] if name == "s3.copy" else [])),
        egress=[{"host":"{entry.host}","port":"{entry.port}"}] if ext else [], determinism="deterministic_effect", idempotency="conditional_write",
        resources={L:{"bytes_per_s":lam_bps,"overhead_s":10}, P:{"bytes_per_s":None if name=="s3.copy" else 50_000_000,"overhead_s":35}}))
planned = [("crypto.encrypt",["encrypt"],"Encrypt a package (deferred: ADR-0012)."),("crypto.decrypt",["encrypt"],"Decrypt a package (deferred: ADR-0012)."),
           ("package.tar",["package"],"tar.gz packaging."),("package.unzip",["transform"],"Safe extraction (zip-slip, bomb defences)."),
           ("s3.move",["housekeeping"],"Copy, verify, delete."),("s3.rename",["housekeeping"],"Rename within a bucket."),("sqs.ack",["integration"],"Acknowledge messages explicitly."),
           ("secret.read",["integration"],"Resolve a secret handle for later rules."),("housekeeping.retention",["housekeeping"],"Tag or expire staging objects."),
           ("validate.schema",["validate"],"CSV/JSON header and schema checks."),("validate.name_pattern",["validate"],"File name pattern checks."),("validate.checksum_match",["validate"],"Sidecar checksum match."),
           ("discover.list",["extract"],"List an SFTP/FTPS/HTTPS server path (recursive)."),("transfer.download",["extract"],"Download from an SFTP/FTPS/HTTPS server.")]
for name, cats, summary in planned:
    ops.append(dict(op=name, version="0.0.0", status="planned", categories=cats, summary=summary))
with open(OUT, "w") as fh:
    fh.write(json.dumps({"catalog_version":"1.0.0","ops":sorted(ops,key=lambda o:o["op"])}, indent=2, sort_keys=True) + "\n")
print(len(ops))
