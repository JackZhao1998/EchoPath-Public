# Skills

Store reusable agent skills in this folder going forward.

Suggested contents over time:

- one folder per skill
- a `README.md` or `SKILL.md` per skill
- examples, prompts, and configuration files specific to each skill

This keeps skill implementations separate from the core agent infrastructure.

Current folders include EchoPath runtime skills used by the core agent and
`echopath-mcp`, an agent-facing usage skill for operating the EchoPath MCP
server safely and tersely from MCP hosts.

Installation and end-user desktop-task skills are shipped under
`packaging/agents/echopath-setup/` and `packaging/agents/echopath/`.
Start with [INSTALL.md](../INSTALL.md); the setup helper installs these into the
selected coding agent's skill directory. Runtime skills here remain part of the
EchoPath package and are not copied wholesale into an agent's global skills.
