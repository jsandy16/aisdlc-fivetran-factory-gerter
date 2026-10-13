# Spike S6 (local part): streaming a large package with bounded memory

Run: `cd packages && FACTORY_SPIKE_BYTES=2e9 uv run pytest factory_runtime/tests/test_spike_s6.py -m spike -s`
(moto server in its own process; the streaming client in another; synthetic, incompressible data in 20 objects).

| Size | Peak RSS of the client | Throughput | Zip |
|---|---|---|---|
| 0.4 GB | 319 MB | 19.8 MB/s | 400,002,342 bytes (members stored: content does not compress) |
| 2.0 GB | 335 MB | 22.0 MB/s | 2,000,002,342 bytes |

Findings (2026-10-13, Python 3.13, 4 vCPU):
- Memory does not grow with the package size (319 MB at 0.4 GB, 335 MB at 2 GB); the bound asserted by the test is
  part size x 3 + 384 MB = 576 MB.
- Throughput is moto-bound (moto materialises whole objects); without network the zip path runs at about 100 MB/s
  stored and about 29 MB/s deflating incompressible data. Incompressible members are now stored (a first-256-KiB
  compressibility test, deterministic), so deflate is spent only where it pays.
- The engine table's `package.zip` 60 MB/s assumption holds for compressible text; for incompressible content that
  is not detected as such it would be optimistic. Calibrate against real S3 in Phase 8 (10 GB, Lambda and pod).
