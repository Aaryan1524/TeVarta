# Contracts

Contracts are versioned boundaries, not implementation details. A producer may
add optional fields only in a new compatible revision; a breaking field removal,
renaming, or semantic change requires a new version and both founders' approval.

- `components/v1/` contains Aaryan's T04 worker/graph draft.
- T03 will add API contracts and mock fixtures without changing these component
  semantics silently.
- `fixtures/` contains safe, synthetic examples; do not place production user,
  token, APNs, source-body, or credential data in either directory.
