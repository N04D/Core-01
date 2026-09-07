# Markdown Website / Git Publisher

Core-01 publishes a channel-specific Markdown variant through the normal event
bus. Register the plugin and route once:

```bash
./venv/bin/python3 plugins/channels/pub_markdown_git.py \
  --register --db "$CORE_DATA/db/events.db"
```

Create `config/markdown_git.json` from
`config/markdown_git.example.json`. The repository must already be a local Git
worktree. Credentials stay outside this file: use an SSH agent, Git credential
manager, or the host's normal Git configuration.

Important options:

| Option | Meaning |
|---|---|
| `enabled` | Enables the channel independently of other plugins. |
| `repository_path` | Absolute or checkout-relative website repository. |
| `content_directory` | Repository-relative Markdown root. |
| `media_directory` | Repository-relative media root. |
| `base_url` / `url_strategy` | Optional canonical URL generation (`posts-slug` or `slug`). |
| `default_branch` | Branch checked before mutation. |
| `commit_enabled` | Create a commit after writing. Defaults to conservative local write. |
| `push_enabled` | Permit an explicit push; it never follows merely from a remote existing. |
| `filename_strategy` | `slug.md`, `date-slug.md`, or `slug/index.md`. |

An event payload uses `title`, `slug`, `body_markdown`, `summary`, `tags`,
`published_at`, `status`, and optional `media` paths. It may instead reference a
Markdown-specific `variant_path`, `draft_file`, or `filepath`.

Dry-run mode produces a complete preview and performs no filesystem or Git
mutation:

```bash
./venv/bin/python3 plugins/channels/pub_markdown_git.py \
  --event_id 123 --db "$CORE_DATA/db/events.db" --dry-run
```

The worker owns queue status. The plugin records the existing publication
ledger using `(event_id, PUBLISH_MARKDOWN_GIT)` idempotency. A confirmed attempt
is returned without another write or commit. Ambiguous push outcomes remain
`UNKNOWN` and are never blindly retried.

`plugins.channels.pub_markdown_git.MarkdownGitReconciler` verifies the target
file hash and recorded local commit. A local commit with an unproven remote
push remains `NEEDS_OPERATOR`; reconciliation never republishes content.
