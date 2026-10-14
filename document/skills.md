# Skill Equipping And Selection

The framework now separates three related concepts:

## Registered skills

Registered skills live in the shared capability registry and can be attached to any agent instance.

## Equipped skills

An agent can be equipped with a specific subset of registered skill names. When an equipped list is set, the agent context only allows those skills to be selected or executed.

## Selected skill

During a run, the agent can select one equipped skill. Selection loads the skill markdown into the run context so the agent can use that content during planning and execution.

The selected skill is exposed through:

- `context.get_selected_skill()`
- `context.get_selected_skill_markdown()`
- `context.state["selected_skill"]`
- the `selected_skill_markdown` field injected into `context.use_skill(...)` payloads

## File-backed skills

Use `MarkdownSkill` for skills backed by a local markdown file. It can point directly to a file or be created from a folder containing `SKILL.md` or `README.md`.

This design keeps the agent core small while making skill instructions first-class planning context.
