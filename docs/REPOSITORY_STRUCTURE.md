# Repository structure

This map keeps product specifications, cross-team contracts, runnable services,
and test evidence separate. It is intentionally small for Week 1; create a
directory only when its owning task adds a tracked artifact.

```text
.
├── .github/                 # PR policy and CI workflows
├── config/                  # versioned, non-secret runtime configuration
├── contracts/               # versioned boundaries shared across tracks
│   └── components/v1/       # Aaryan's draft worker/graph contracts (T04)
├── docs/                    # workflow, task records, and architecture notes
├── fixtures/                # deterministic, non-production contract examples
│   └── components/v1/       # T04 fixture set
├── graph/                   # curated edge files and graph-specific artifacts (T14+)
├── backend/                 # FastAPI, Celery, migrations, and Python tests (T02)
├── ios/                     # SwiftUI application and Xcode project (T02)
├── infra/                   # non-secret deployment and environment definitions (T06)
├── scripts/                 # deterministic local validation helpers
├── TEVARTA_MVP_BUILD_v2.1.md
├── TEVARTA_MVP_EXECUTION_PLAN.md
├── decisions.md
└── AGENTS.md
```

## Ownership and placement rules

| Area | Purpose | Initial owner |
|---|---|---|
| `contracts/` | Stable, language-neutral schemas that more than one component consumes | Shared; Aaryan authors T04, Aaditya authors T03 API contracts |
| `fixtures/` | JSON/YAML examples used by contract, backend, and Swift decoding tests | Shared |
| `backend/` | FastAPI/Celery application code, Alembic migrations, and Python tests | Aaditya creates the skeleton in T02; Aaryan owns graph/pipeline modules thereafter |
| `ios/` | Xcode project, SwiftUI targets, and Swift tests | Aaditya |
| `graph/` | Curated-edge YAML and graph validation/load evidence | Aaryan, with Aaditya's evidence acceptance for curated edges |
| `config/` | Checked-in, non-secret operational configuration | Task owner; secrets never belong here |
| `infra/` | Reviewable, non-secret infrastructure definitions | Aaditya (T06) |
| `scripts/` | Dependency-light validation tools used by local and CI checks | Task owner |

Keep product source-of-truth documents at the repository root until the frozen
MVP is revised. Do not add empty placeholder directories, credentials, generated
build products, local databases, or copied source bodies.
