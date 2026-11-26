# Secret files are masked whole, chosen only by configured glob patterns

Every Secret file, including credential-bearing files inside `.git` such as `.git/config`, is selected by a Secret pattern in configuration and masked whole (empty and read-only). The sandbox never inspects or rewrites file contents. We chose this to keep a single, predictable mechanism: what's masked is exactly what `check` lists from the patterns, and users adjust behaviour by editing globs, not by relying on built-in special cases.

## Considered Options

- **Sanitized copy of `.git/config`** (strip tokens from URLs, drop `extraheader` and `credential.*`, mount the cleaned copy): rejected. It's content parsing code that must keep up with git's config format, and it's a second masking mechanism to explain and test.

## Aliases: symlinks and hard links

Masking follows every alias, so a Secret can't be reached under another name:

- **Symlink with a Secret name** (`.env` → `config/real.env`): the target inside the Workspace is masked. Docker resolves a mount destination through symlinks, so masking is applied at the resolved real path.
- **Symlink with a harmless name pointing at a Secret** (`config/link` → `../.env`): already safe, because the target is masked.
- **Symlink pointing outside the Workspace**: nothing to mask, since the target isn't mounted. Inside the container it dangles. Discovery never follows it.
- **Hard link** (`notes.txt` shares an inode with masked `.env`): every directory entry with that inode is masked separately, because each hard link is its own path.

`check` shows every alias and why it is masked, e.g. `MASKED notes.txt (hard link to .env)`.

## Consequences

- With `**/.git/config` masked, git inside the sandbox loses remotes, upstream tracking and any repo-level settings. Local status, diff, add, commit and log are expected to still work for ordinary repos, but this must be verified by tests. Repos that need `extensions.*` (e.g. SHA-256 object format, partial clone) may not work. A user who needs those can remove the pattern for that project, accepting that any token in the config becomes visible.
- Git history, packed objects and other `.git` content stay readable; secrets committed to history remain exposed (known limit, per ADR-0001).
- Discovery skips dependency and build-output directories, by default `node_modules/`, `vendor/`, `dist/` and `libs/` (configurable as Skip patterns). Files under them are never masked, even if they match a Secret pattern; this is a known limit that `check` must state. `.git/` is not skipped, so patterns like `**/.git/config` still apply.
