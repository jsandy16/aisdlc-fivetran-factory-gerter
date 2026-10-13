# toolchain/ (Phase 2+)

The toolchain image (Dockerfile) and `toolchain.lock.json` pin every build-time tool, so generation is byte-identical
on any machine ([ADR-0017](../docs/adr/0017-determinism.md)).
