# Contributing to TENIR-Gov

Thank you for your interest in TENIR-Gov. This is a research software publication
accompanying a SoftwareX manuscript. Contributions that improve reproducibility,
documentation, or test coverage are especially welcome.

## How to Contribute

### Reporting Issues

Please use [GitHub Issues](https://github.com/skiredj-prog/tenir_governance/issues) for:
- Bug reports (include Python version, OS, and the full traceback)
- Reproducibility failures (include the docker compose or pytest command used)
- Documentation gaps or inaccuracies

### Submitting Changes

1. **Fork** the repository and create a branch from `main`.
2. **Run the full test suite** before opening a pull request:
   ```bash
   pytest tests/ r4/tests/ \
     r5_hardened/IRON_OMEGA_R5/test_r5_all.py \
     r5_hardened/IRON_OMEGA_R5/test_institutional_hardening.py \
     r5_wired/test_r5_governance_integration.py \
     --cov=tenir_governance
   ```
3. **Validate the policy contract** (required for any change to `tenir_governance/`):
   ```bash
   tenir-validate --policy default
   tenir-validate --policy partner_a
   tenir-validate --policy partner_b
   ```
4. **Run copy-lint** if you modify any `.md` or `.html` files:
   ```bash
   python -m tenir_governance.copy_lint .
   ```
5. Open a pull request against `main`. The CI gate must be green before merge.

### Key Invariants (Do Not Break)

- **POL-001**: All membrane decisions must flow through `PolicyEngine.evaluate_membrane()`. No local threshold literals.
- **NOM-001**: `CAVEFieldNames.EXPANSION` must expand to Context / Action / Value / Effect.
- **NOM-002**: `OperatingModeNames.R4_TO_R5` must map all four R4 modes.
- Policy fingerprint for `PolicyEngine.default()` is `d083e0b82a16c04d`. Any intentional policy change must update this fingerprint and bump the policy version string.

### Coding Standards

- Python 3.10+ with type hints where practical.
- Docstrings for all public API functions and classes.
- New golden cases go in `tenir_governance/regression_corpus.py` — see the existing `GoldenCase` pattern.
- No banned terms in public-facing text (`PUBLIC_BANNED_TERMS` in `nomenclature.py`).

## Code of Conduct

Be professional and constructive. This project is associated with an academic publication;
interactions should reflect that standard.

## Licence

By submitting a contribution you agree that your work will be licensed under the
[Apache License 2.0](LICENSE) that covers this project.
