# CI setup and verification

The repository supplies three required CI workflows:

- **Backend contract:** lint, fixture/model validation, mock smoke tests, and a
  generated-OpenAPI drift check.
- **iOS:** shared-fixture Swift decoding and a simulator app build.
- **Dependency review:** dependency changes are reviewed on pull requests;
  Dependabot keeps Python and Actions dependencies current.

After the first push, configure `Backend contract / test`, `Backend contract /
required-check-guard`, and `iOS / build` as required status checks for the
protected integration branch. GitHub branch-protection settings are account
state, so they cannot be represented or verified purely in this checkout.

To verify that the guard actually blocks a merge candidate, run this only in a
throwaway branch or CI job:

```sh
TEVARTA_CI_FORCE_FAILURE=1 ./scripts/verify_required_checks.sh
```

It exits non-zero by design. `backend/tests/test_ci_guard.py` proves that
behavior without making normal CI fail.
