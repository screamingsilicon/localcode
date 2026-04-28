# Skill Writing Guide

A **Skill** is a self-contained Markdown file (`SKILL.md`) that teaches the agent how to perform a specific task. Skills are discovered automatically and shown to the model via the `invoke_skill` tool.

---

## File Location

Place `SKILL.md` inside a directory that represents the skill name:

| Scope | Path | Priority |
|---|---|---|
| **Personal** | `~/.localcode/skills/<skill-name>/SKILL.md` | Lower (loaded first) |
| **Project** | `.localcode/skills/<skill-name>/SKILL.md` | Higher (overrides personal) |
| **Project** | `.agents/skills/<skill-name>/SKILL.md` | Higher (overrides personal) |

The directory name becomes the skill name if no `name` is provided in frontmatter.

---

## File Structure

Every `SKILL.md` has two parts:

1. **YAML-like frontmatter** (between `---` markers) — metadata
2. **Body** (after the closing `---`) — instructions for the agent

```markdown
---
name: my-skill
description: A short one-line description of what this skill does.
disable-model-invocation: false
user-invocable: true
---

The body contains detailed instructions that are injected into the
agent's context when the skill is invoked.
```

---

## Frontmatter Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | directory name | Skill identifier (lowercase, hyphens). Used in `invoke_skill` calls. |
| `description` | string | first paragraph of body | Shown in the "Available Skills" list to the model. Keep it to one line. |
| `disable-model-invocation` | boolean | `false` | If `true`, the skill is hidden from the model's available skills list. Only a human can invoke it. Useful for setup/configuration skills. |
| `user-invocable` | boolean | `true` | If `false`, only the model can invoke it (not the user via CLI). |

### Name Rules

- The name is **lowercased** and underscores are converted to hyphens automatically.
- Example: `Code_Reviewer` becomes `code-reviewer`.

---

## Body

The body is Markdown text that gets injected into the agent's system context when the skill is loaded. Write it as **instructions** to the agent — what it should do, how to do it, and what output format to produce.

### Argument Substitution

The `invoke_skill` tool accepts an optional `arguments` string. These are substituted into the body at load time:

| Placeholder | Meaning |
|---|---|
| `$ARGUMENTS` | Full raw argument string |
| `$0` | First argument |
| `$1` | Second argument |
| `$N` | Nth argument (0-indexed) |
| `$ARGUMENTS[0]` | Same as `$0` |
| `$ARGUMENTS[1]` | Same as `$1` |

Arguments are split on whitespace, respecting quoted strings.

**Example invocation:** `invoke_skill("code-reviewer", "src/main.py")`

In the body, `$0` or `$ARGUMENTS` would resolve to `src/main.py`.

---

## Complete Example

```markdown
---
name: unit-test-writer
description: Generates comprehensive unit tests for a given Python file.
---

You are now acting as a Senior QA Engineer.

Your goal is to write unit tests for the file: $0

Follow these steps:
1. Read the target file using `get_repo_map` and `cat`.
2. Identify all public functions and classes.
3. Create a test file at `tests/test_$1.py` using `write_file`.
4. Use `pytest` and `unittest.mock` for testing.
5. Cover happy paths, edge cases, and error conditions.
6. Run the tests with `python -m pytest` to verify they pass.

Report any issues found during test execution.
```

Invocation: `invoke_skill("unit-test-writer", "src/utils.py utils")`

---

## Tips

- **Keep descriptions short** — they appear in a bulleted list shown to the model on every turn.
- **Be specific in the body** — tell the agent exactly which tools to use and in what order.
- **Use `$0`, `$1`** for concise argument references; use `$ARGUMENTS` when you need the full string.
- **Set `disable-model-invocation: true`** for skills that should only be triggered manually (e.g., dangerous operations, setup scripts).
- **One skill per task** — keep skills focused and composable rather than writing one massive catch-all skill.
