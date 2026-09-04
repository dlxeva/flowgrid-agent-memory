# Repository governance

The intended `main` rules are:

- changes arrive through pull requests;
- the five CI checks are required: Core Python 3.11/3.12/3.13, MCP Python 3.11,
  and the Container contract;
- force pushes and branch deletion are disabled;
- workflow and release changes are owned by `@dlxeva`;
- releases are created only by `.github/workflows/release.yml` after the full
  acceptance, fresh-install, container, evidence, and attestation gates pass.

`CODEOWNERS` records review ownership in the repository. GitHub branch rules are
repository-administration state and must mirror this document.
