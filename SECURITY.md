# Security policy

AgentIC is local-only software: no accounts, no servers to breach, no data
leaves the machine except the user's own model API calls.

## Threat model

- The engine binds loopback only and trusts the local OS user. There is
  no authentication boundary to attack; filesystem permissions are it.
- The real risks are the agent executing shell commands and reading files.
  Execution is jailed to the workspace (`exec_service`, `run_bash` cwd
  pinning); report bugs that escape it as **high severity**.
- The desktop renderer is untrusted web content and must only reach the
  backend through the `window.api` preload surface.

## Reporting

Do not open public issues for vulnerabilities. Email
agent.ic@buildstack.live with steps to reproduce. Expect a fix or a
response within 7 days; coordinated disclosure after a patch release.
