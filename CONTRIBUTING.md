# Contributing to NYRA

NYRA is local-first and security-sensitive. Keep identity, LLM, memory, events, voice, tools, integrations and UI modular.

Before opening a change:

1. create a focused branch;
2. keep secrets and operator data outside Git;
3. preserve `system_shell`, `remote_shell`, Grounding, Credential Broker and Approval Gate boundaries;
4. add unit/integration/security tests proportional to the change;
5. run backend tests, frontend tests/build and `git diff --check`;
6. document architecture, configuration or security changes.

Never commit `.env`, credentials, private configs, databases, logs, traces, memory, knowledge corpora, PDFs, recordings, models, screenshots from an operator session, SelfDev worktrees or backup bundles.

Security reports must follow [SECURITY.md](SECURITY.md).
