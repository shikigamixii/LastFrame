# Project rules for Claude Code

## Branch ownership

The owner's branches are:

- `main`
- `feature/jellyfin`
- `feature/plex`

**Any branch with `claude` in its name was NOT created by the owner.**
Do not develop on, push to, or open PRs from `claude/*` branches.
All work must be done on one of the owner's branches above.

If a Claude Code session was started on a `claude/*` branch by the
harness, treat the corresponding owner branch as the rightful target
and apply the work there instead.

## Working style

- Double-check your work before reporting it as done: re-read the
  changes, confirm they match the request, and verify assumptions
  against the actual code rather than memory.
- Ask clarifying questions whenever requirements are ambiguous,
  multiple reasonable interpretations exist, or a decision could
  have non-obvious consequences. Prefer asking over guessing.
- Don't break anything that's already working. Before making a
  change, understand the existing behavior the code is providing,
  and preserve it unless the change explicitly calls for replacing
  it. Verify nearby/related features still work after the change.
- Keep `feature/jellyfin` and `feature/plex` in sync. When a change
  is made on one, port the equivalent change to the other in the
  same session (adapting for the backend's API differences). If a
  change genuinely doesn't apply to the other backend, say so
  explicitly rather than silently skipping it.
- At the start of every session, fetch and fast-forward `main`,
  `feature/jellyfin`, and `feature/plex` to match `origin` before
  doing any work, so local branches always reflect the latest
  pushed state.
- Never include secrets, API keys, tokens, passwords, personal
  email addresses, or any other private information in code,
  comments, commit messages, PR descriptions, branch names, or
  anywhere else that ends up on GitHub. If the owner pastes
  something sensitive in chat, treat it as scratch input only —
  use it to do the work, but never echo it into a file, a commit,
  or a remote artifact. If a secret has to be referenced in code,
  read it from an environment variable or a local config file
  that is gitignored.
