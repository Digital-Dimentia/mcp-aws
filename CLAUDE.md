# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Project: mcp-aws

Read-only AWS MCP server over stdio, built to sit behind an MCP gateway.

### The invariant

**Coverage lives in the catalog, never in the tool surface.** There are exactly five
tools and there will stay exactly five; a sixth is a design failure, not a feature. New
AWS capability is a YAML entry in `src/mcp_aws/catalog/views/`.

The reason is context cost: tool schemas are resent every turn, so a per-operation tool
surface would consume the model's budget before it asks anything. `tests/test_surface.py`
asserts both the tool count and the serialized schema size — if it fails, the design has
regressed.

Resources, resource templates, prompts and completions are *outside* that budget — they
are fetched on demand, not resent — so that is where anything the tool surface cannot
afford belongs. New capability for a gateway is a listing/template pair in `resources.py`,
never a sixth tool.

## Build & Test

```bash
uv sync
uv run pytest        # fully offline; AWS is stubbed with botocore Stubber
uv run mcp-aws       # speaks MCP over stdio
```

Never add a test that needs credentials or network. Use the `stub_client` fixture.

## Architecture Overview

| Path | Role |
|---|---|
| `server.py` | MCPServer wiring, the five tool wrappers, stdio entrypoint |
| `tools.py` | Tool bodies — thin; no boto3 here |
| `catalog/models.py` | The YAML schema as pydantic models |
| `catalog/loader.py` | Discovery + validation; where read-only is enforced |
| `catalog/engine.py` | The only module that executes AWS calls for a query |
| `catalog/views/*.yaml` | The catalog itself |
| `resources.py` | Listing/template pairs; the gateway pairing rule lives in its docstring |
| `vocabulary.py` | The values a picker and a completion both read from |
| `completions.py` | `completion/complete` over those same vocabularies |
| `prompts.py` | Two prompts whose argument names are the vocabulary's variables |
| `aws/profiles.py` | Named profiles → accounts |
| `aws/regions.py` | Regions from bundled endpoint data — offline on purpose |
| `budget.py` | Size caps and lossless pagination cursors |

## Conventions & Patterns

- **Nothing writes to stdout.** It is the JSON-RPC channel; one stray byte kills the
  session with no useful error. All logging goes to stderr via `logging_setup`.
- **Read-only is structural.** The loader rejects non-read operations, unknown
  operations, bad parameter targets and denylisted sensitive reads *at startup*. Do not
  add a runtime bypass, and do not add a generic "call any API" tool.
- **A broken catalog fails the build, not the user.** `tests/test_catalog.py` validates
  every shipped view against real botocore models. Run it after touching any YAML.
- **Truncation must stay lossless.** Items carry their page of origin so a cursor can
  resume mid-page. `engine._next_page_token` uses botocore internals deliberately —
  `PageIterator.resume_token` is only populated on botocore's own MaxItems truncation,
  and its MaxItems counts result-key entries, not projected items.
- **A listing is free to read.** A gateway reads every listing resource whenever someone
  opens the server, so `vocabulary.py` may only use local config, bundled endpoint data
  and the in-memory catalog. Live calls belong behind a member URI. `test_resources.py`
  fails loudly if a listing ever creates an AWS client.
- **Resource handlers raise; tool wrappers return.** A resource handler raises
  `ResourceNotFoundError`/`ResourceError` from `mcp.server.mcpserver.exceptions` — an
  `McpAwsError` escaping one is treated by the SDK as a crash and its message is withheld
  from the client. `_guarded` keeps doing the opposite for tools, which report an error as
  content.
- **URIs pair by prefix.** A template's fixed prefix, up to its first `{`, must be its
  listing's URI, or the picker silently goes empty. `test_resources.py` implements the
  rule rather than listing the URIs, so a rename fails there.
- **Tool wrappers keep their signatures.** `_guarded` uses `functools.wraps` because the
  input schema is derived from the signature; losing it publishes `(*args, **kwargs)`.
