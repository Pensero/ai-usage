# git ai-usage

Record the AI coding-agent usage of each commit in git notes: tokens, API-equivalent cost and
edited lines, per model. It reads the local Claude Code transcripts (`~/.claude/projects`) and Codex
rollout files (`~/.codex/sessions`), so it needs no account, no telemetry and no server.

```sh
curl -fsSL https://raw.githubusercontent.com/Pensero/ai-usage/main/install.sh | sh
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
  claude-code claude-opus-4-1                              9.00 $  out=48.0k
  claude-code claude-sonnet-4-5                            1.31 $  out=8.1k
  codex gpt-5.6-sol                                        1.00 $  out=6.0k
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
  or one of its worktrees. For Codex, that is the `cwd` of the turn in the rollout file.
- **Storage.** Git notes in `refs/notes/ai-usage`. Commit SHAs do not change, so past commits can
  get notes. One line per tool and model, and one line per tool for edited lines:

  ```
  claude-code model=claude-opus-4-1 in=1200 out=54000 cache_read=23000000 cache_write=410000 cost_usd=14.1200 price_src=litellm from=1791000000 to=1791003600
  claude-code added=212 deleted=31 from=1791000000 to=1791003600
  codex model=gpt-5.6-sol in=3100 out=900 cache_read=410000 cache_write=0 cost_usd=0.1940 price_src=litellm+models.dev from=1791000000 to=1791003600
  ```

  To sum, add the numeric keys of the unique lines. `from`/`to` (Unix seconds) make a line unique.
  `in` is the input that was not read from or written to the cache, for both tools.
- **Rewrites.** `install` sets `notes.rewriteRef`, so notes follow `commit --amend` and `rebase`.
  A squash keeps the lines of all the squashed commits. An amend copy counts once.
- **Sharing.** Git does not push notes by default. The `pre-push` hook pushes them after it
  merges the remote notes (`cat_sort_uniq`). To see the notes of other people: `git ai-usage fetch`.
- **Cost** is the API price of the tokens, per request or response. A Claude Haiku 5.5 request
  above 100K prompt tokens pays its higher rate. Codex gets the long-context rate above 272K input
  tokens and the rate of the processing mode (Fast, Flex). On a subscription plan (or a ChatGPT
  seat) you pay a flat fee instead.
- **Prices** come from, in order:
  1. [LiteLLM's price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json).
  2. [models.dev](https://models.dev), for the models and long-context rates LiteLLM lacks.
  3. The tables built into the script, from the
     [Anthropic](https://platform.claude.com/docs/en/about-claude/pricing) and
     [OpenAI](https://developers.openai.com/api/docs/pricing) pricing pages.

  `backfill` and `setup` download the first two at most once a day into
  `~/.cache/git-ai-usage/prices.json` (or `$XDG_CACHE_HOME`). The post-commit hook only reads that
  file, so a commit never waits on the network. `--offline` or `GIT_AI_USAGE_OFFLINE=1` uses only
  the built-in tables. `price_src` on each note line says where its rates came from: `litellm`,
  `models.dev`, `builtin`, or `none` for a model with no price, which costs 0.

## Limits

- Claude Code deletes transcripts after `cleanupPeriodDays` (default 30 days). A backfill cannot
  go back further than the transcripts that remain. To keep more, set a larger value in
  `~/.claude/settings.json`. Codex keeps its rollout files in `$CODEX_HOME` (default `~/.codex`),
  `sessions/` and `archived_sessions/`.
- The match is an estimate. When you work on two branches in the same folder at the same time,
  their usage mixes. When you commit from a different email, those commits are not bounds.
- A squash merge on the server makes a new commit with no note. The pull request commits keep
  their notes, so `git ai-usage show base..head` still works.
- The notes are visible to everyone who can read the repository.
- Ephemeral Codex background threads write no rollout file, so they are not counted. The
  Codex OTel export includes them, so it shows a little more (about 0.5% of the tokens on a day we
  measured). The warmup response at the start of a session, which has no output, is not counted either.
- Only Claude Code and Codex for now.

## Requirements

`git` and `python3` 3.9 or later. No other dependencies. macOS has both after
`xcode-select --install`. You do not need to change your `PATH`: when `~/.local/bin` is not on it,
the installer adds the git alias `ai-usage`.

## Development

```sh
python3 -m unittest discover -s tests -v
```
