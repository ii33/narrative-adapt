# Encoding helper provenance

`encode_write.py` is copied unchanged from `@local/dsh-agentkit` 1.4.8,
`lib/encode_write.py`, copyright 2026 ii33, MIT (see LICENSE).
Only its `--encode-only` mode is invoked by `narrative.host_text`, through
the current Python executable, argument arrays and UTF-8 stdin. Its file-writing
mode is not used. No Node runtime, bundle installation or external path is needed.

The small Python codec/EOL adapter is informed by that package's
`lib/encoding.js`. It deliberately excludes lossy decoding and reports ambiguous
legacy encodings instead of silently selecting one.

Vendored original SHA-256: `f3f399b4ec4e213ed2c64a89e0263b3fa4a427a658ff4fe52716ef1120b41633`.
