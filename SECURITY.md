# Security policy

## Supported versions

Security fixes are maintained for the latest published minor release.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not include production credentials, private network topology, personal documents, memory databases, traces, recordings or exploit data that belongs to another person.

Do not open a public issue for an unpatched vulnerability or a leaked credential. If a credential may have been exposed, revoke or rotate it before sharing a redacted report.

## Security boundaries

- local shell execution is owned by `system_shell`;
- SSH is owned by `remote_shell` and the Trusted Host Registry;
- sensitive actions require an exact, single-use Approval Gate decision;
- credentials remain in the Credential Broker;
- web, document, tool and memory content never receives system-instruction authority;
- runtime data, local configs, knowledge corpora, models and audio are not release artifacts.

See [docs/security.md](docs/security.md) for the architecture and threat controls.
