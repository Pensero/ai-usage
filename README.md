# git ai-usage

Record the AI coding-agent usage of each commit in git notes: tokens, API-equivalent cost and
edited lines, per model. It reads the local Claude Code transcripts (`~/.claude/projects`), so it
needs no account, no telemetry and no server.

```sh
curl -fsSL https://raw.githubusercontent.com/Pensero/ai-usage/main/install.sh | sh
```

Or install from a clone:

```sh
git clone https://github.com/Pensero/ai-usage ~/.ai-usage && ~/.ai-usage/install.sh
```

Run it inside a repository. It installs the hooks, asks to backfill your past commits, and asks
to push the notes.

## Use

```sh
git ai-usage show              # this branch (default branch..HEAD), per commit and per model
git ai-usage show HEAD         # one commit
git ai-usage show main..feat   # any range, for example a pull request
git ai-usage show --json       # machine-readable
git log                        # the notes show under each commit
```

```
AI usage for origin/main..HEAD

commit           in     out  c.read c.write   cost $  +lines  -lines  subject
28683d2d60     1.2k   62.1k    6.4M  122.0k    11.31     212      31  Add retry to the sync task
f8cdc211b8        0       0       0       0     0.00       0       0  Fix the retry test
total          1.2k   62.1k    6.4M  122.0k    11.31     212      31

by model:
  claude-opus-4-1                             10.00 $  out=54.0k
  claude-sonnet-4-5                            1.31 $  out=8.1k
```

| Command | What it does |
| --- | --- |
| `git ai-usage setup` | `install`, then offers `backfill` and `push` |
| `git ai-usage install` | Adds the `post-commit` and `pre-push` hooks to this repository. Existing hooks stay. |
| `git ai-usage backfill` | Writes notes for your past commits that have none (`--force` rewrites them, `--path` adds an earlier path of the clone, `--author` uses another email) |
| `git ai-usage push` / `fetch` | Sends or gets `refs/notes/ai-usage`, merging the notes of other people |
| `git ai-usage uninstall` | Removes the hooks. The notes stay. |

## How it works

- **Window.** A commit gets the usage recorded between your previous commit and its author date.
  Only your commits (`user.email`) are bounds, and every usage record goes to exactly one commit.
  Usage that you did not commit goes to your next commit in that repository.
- **Repository match.** A usage record counts when its working directory is inside the repository
  or one of its worktrees.
- **Storage.** Git notes in `refs/notes/ai-usage`. Commit SHAs do not change, so past commits can
  get notes. One line per model, and one line for edited lines:

  ```
  claude-code model=claude-opus-4-1 in=1200 out=54000 cache_read=23000000 cache_write=410000 cost_usd=14.1200 from=1791000000 to=1791003600
  claude-code added=212 deleted=31 from=1791000000 to=1791003600
  ```

  To sum, add the numeric keys of the unique lines. `from`/`to` (Unix seconds) make a line unique.
- **Rewrites.** `install` sets `notes.rewriteRef`, so notes follow `commit --amend` and `rebase`.
  A squash keeps the lines of all the squashed commits. An amend copy counts once.
- **Sharing.** Git does not push notes by default. The `pre-push` hook pushes them after it
  merges the remote notes (`cat_sort_uniq`). To see the notes of other people: `git ai-usage fetch`.
- **Cost** is the API price of the tokens, from the
  [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing), per request (a
  Haiku 5.5 request above 100K prompt tokens pays its higher rate). On a subscription plan you pay a
  flat fee instead.

## Limits

- Claude Code deletes transcripts after `cleanupPeriodDays` (default 30 days). A backfill cannot
  go back further than the transcripts that remain. To keep more, set a larger value in
  `~/.claude/settings.json`.
- The match is an estimate. When you work on two branches in the same folder at the same time,
  their usage mixes. When you commit from a different email, those commits are not bounds.
- A squash merge on the server makes a new commit with no note. The pull request commits keep
  their notes, so `git ai-usage show base..head` still works.
- The notes are visible to everyone who can read the repository.
- Only Claude Code for now.

## Requirements

`git` and `python3` 3.9 or later. No other dependencies.

## Development

```sh
python3 -m unittest discover -s tests -v
```
