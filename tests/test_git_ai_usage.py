"""End-to-end tests: real git repositories and fake Claude Code and Codex transcripts in a temporary HOME."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "git-ai-usage"
loader = importlib.machinery.SourceFileLoader("git_ai_usage", str(SCRIPT))
spec = importlib.util.spec_from_loader("git_ai_usage", loader)
tool = importlib.util.module_from_spec(spec)
sys.modules["git_ai_usage"] = tool
loader.exec_module(tool)

EMAIL = "dev@example.com"
T0 = 1_790_000_000  # commit times in the tests are T0 + n * 1000


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text(f"[user]\n\temail = {EMAIL}\n\tname = Dev\n[init]\n\tdefaultBranch = main\n")
        self.env_backup = dict(os.environ)
        os.environ.update({
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "CODEX_HOME": str(self.home / ".codex"),
        })
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.cwd_backup = os.getcwd()
        os.chdir(self.repo)
        self.git("init", "-q")
        self.messages = 0

    def tearDown(self):
        os.chdir(self.cwd_backup)
        os.environ.clear()
        os.environ.update(self.env_backup)

    def git(self, *args, env=None, cwd=None, stdin=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or os.getcwd(), env={**os.environ, **(env or {})},
            capture_output=True, text=True, input=stdin,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def commit(self, n: int, message: str = None, email: str = EMAIL, amend: bool = False, cwd=None) -> str:
        cwd = Path(cwd or os.getcwd())
        stamp = f"@{T0 + n * 1000} +0000"
        (cwd / f"file{n}.txt").write_text(f"{n}\n")
        self.git("add", "-A", cwd=cwd)
        args = ["commit", "-q", "-m", message or f"commit {n}"] + (["--amend"] if amend else [])
        self.git(*args, env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_EMAIL": email}, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd).strip()

    def transcript(self, name: str = "session") -> Path:
        folder = self.home / ".claude" / "projects" / tool.encode_project(os.path.realpath(self.repo))
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{name}.jsonl"

    def call(self, path: Path, when: float, model="claude-opus-4-1", out=100, cwd=None, message_id=None, cache_read=0):
        self.messages += 1
        record = {
            "type": "assistant", "timestamp": iso(when), "cwd": str(cwd or self.repo), "uuid": f"u{self.messages}",
            "message": {"id": message_id or f"m{self.messages}", "model": model,
                        "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": 0}},
        }
        with path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def edit(self, path: Path, when: float, lines: int):
        self.messages += 1
        record = {"type": "user", "timestamp": iso(when), "cwd": str(self.repo), "uuid": f"e{self.messages}",
                  "toolUseResult": {"type": "create", "content": "\n".join(["x"] * lines)}}
        with path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def rollout(self, thread: str, cwd=None, model="gpt-5.6-sol", forked_from=None, folder="sessions") -> Path:
        path = self.home / ".codex" / folder / "2026" / "10" / "16" / f"rollout-2026-10-16T10-00-00-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"id": thread, "timestamp": iso(T0), "cwd": str(cwd or self.repo)}
        if forked_from:
            meta["forked_from_id"] = forked_from
        self.codex_line(path, T0, "session_meta", meta)
        self.codex_line(path, T0, "turn_context", {"cwd": str(cwd or self.repo), "model": model})
        return path

    def codex_line(self, path: Path, when: float, kind: str, payload: dict):
        with path.open("a") as handle:
            handle.write(json.dumps({"timestamp": iso(when), "type": kind, "payload": payload}) + "\n")

    def response(self, path: Path, when: float, response_id: str, input=1000, cached=0, out=100, written=0):
        usage = {"input_tokens": input, "cached_input_tokens": cached, "cache_write_input_tokens": written,
                 "output_tokens": out, "reasoning_output_tokens": 0, "total_tokens": input + out}
        self.codex_line(path, when, "token_usage_record", {"response_id": response_id, "usage": usage})

    def codex_note(self, sha: str) -> dict:
        return {entry.get("model"): entry for entry in self.note(sha) if entry["_tool"] == "codex"}

    def note(self, sha: str) -> list[dict]:
        return tool.parse_note(tool.read_note(sha))

    def totals(self, revision: str):
        rev_args = [revision] if ".." in revision else ["-1", revision]
        return tool.summarize(tool.notes_by_commit(rev_args))


class BackfillTest(Sandbox):
    def test_usage_goes_to_the_next_own_commit(self):
        path = self.transcript()
        first = self.commit(1)
        self.call(path, T0 + 1500, out=100)
        self.call(path, T0 + 1600, out=50, model="claude-sonnet-4-5")
        self.edit(path, T0 + 1700, lines=3)
        second = self.commit(2)
        self.call(path, T0 + 2500, out=7)
        third = self.commit(3)
        self.call(path, T0 + 3500, out=9)  # after the last commit: not committed yet

        self.assertEqual(tool.backfill(EMAIL, quiet=True), 2)

        self.assertEqual(self.note(first), [])
        lines = {entry.get("model"): entry for entry in self.note(second)}
        self.assertEqual(lines["claude-opus-4-1"]["out"], "100")
        self.assertEqual(lines["claude-sonnet-4-5"]["out"], "50")
        self.assertEqual(lines[None]["added"], "3")
        self.assertEqual(lines["claude-opus-4-1"]["from"], str(T0 + 1000))
        self.assertEqual(lines["claude-opus-4-1"]["to"], str(T0 + 2000))
        self.assertEqual(self.note(third)[0]["out"], "7")

    def test_ignores_other_folders_other_authors_and_partial_lines(self):
        path = self.transcript()
        self.commit(1)
        self.call(path, T0 + 1100, out=5, message_id="same")
        self.call(path, T0 + 1101, out=40, message_id="same")  # a later line of the same message
        self.call(path, T0 + 1200, out=999, cwd=self.tmp / "elsewhere")
        self.commit(2, email="someone@else.com")
        self.call(path, T0 + 2500, out=1)
        mine = self.commit(3)

        tool.backfill(EMAIL, quiet=True)

        # Someone else's commit is not a window bound: commit 3 gets everything since commit 1.
        entry = self.note(mine)[0]
        self.assertEqual(entry["out"], "41")
        self.assertEqual(entry["from"], str(T0 + 1000))

    def test_notes_and_stash_commits_are_not_window_bounds(self):
        path = self.transcript()
        first = self.commit(1)
        self.call(path, T0 + 1100, out=5)
        stamp = {"GIT_AUTHOR_DATE": f"@{T0 + 1200} +0000", "GIT_COMMITTER_DATE": f"@{T0 + 1200} +0000"}
        os.environ.update(stamp)
        tool.write_note(first, "manual\n")
        (self.repo / "wip.txt").write_text("wip")
        self.git("stash", "-u", "-q")
        for key in stamp:
            del os.environ[key]
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual(self.note(sha)[0]["out"], "5")

    def test_cost_uses_the_model_rates(self):
        path = self.transcript()
        self.commit(1)
        self.call(path, T0 + 1500, out=1_000_000, model="us.anthropic.claude-opus-4-1-20250805-v1:0")
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        # 10 input tokens at $15/M plus 1M output tokens at $75/M.
        self.assertAlmostEqual(float(self.note(sha)[0]["cost_usd"]), 75.00015, places=3)

    def test_keeps_existing_notes_unless_forced(self):
        path = self.transcript()
        self.commit(1)
        self.call(path, T0 + 1500)
        sha = self.commit(2)
        tool.write_note(sha, "manual\n")
        self.assertEqual(tool.backfill(EMAIL, quiet=True), 0)
        self.assertEqual(tool.read_note(sha), "manual\n")
        self.assertEqual(tool.backfill(EMAIL, force=True, quiet=True), 1)
        self.assertEqual(self.note(sha)[0]["out"], "100")

    def test_earlier_path_of_the_repository(self):
        old = self.tmp / "old-clone"
        path = self.transcript()
        self.commit(1)
        self.call(path, T0 + 1500, cwd=old)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual(self.note(sha), [])
        tool.backfill(EMAIL, extra_paths=[str(old)], quiet=True)
        self.assertEqual(self.note(sha)[0]["out"], "100")


class CodexTest(Sandbox):
    def test_usage_and_patches_go_to_the_commit_window(self):
        path = self.rollout("t1")
        self.commit(1)
        self.response(path, T0 + 1500, "r1", input=1000, cached=400, out=100)
        patch = {"type": "item_completed", "item": {"type": "FileChange", "id": "p1", "status": "completed", "changes": {
            "/repo/a.py": {"type": "add", "content": "a\nb\nc\n"},
            "/repo/b.py": {"type": "update", "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n"},
        }}}
        self.codex_line(path, T0 + 1600, "event_msg", patch)
        self.codex_line(path, T0 + 1700, "event_msg", patch)  # the same item again
        second = self.commit(2)
        self.response(path, T0 + 2500, "r2", out=7)
        third = self.commit(3)

        tool.backfill(EMAIL, quiet=True)

        lines = self.codex_note(second)
        usage, edited = lines["gpt-5.6-sol"], lines[None]
        self.assertEqual((usage["in"], usage["cache_read"], usage["out"]), ("600", "400", "100"))
        self.assertEqual((edited["added"], edited["deleted"]), ("4", "1"))
        self.assertEqual(self.codex_note(third)["gpt-5.6-sol"]["out"], "7")

    def test_skips_warmups_other_folders_and_copies(self):
        path = self.rollout("t1")
        self.commit(1)
        self.response(path, T0 + 1100, "warmup", out=0)
        self.response(path, T0 + 1200, "r1", out=10)
        self.response(path, T0 + 1300, "r1", out=10)  # the same response again
        self.codex_line(path, T0 + 1400, "turn_context", {"cwd": str(self.tmp / "elsewhere"), "model": "gpt-5.6-sol"})
        self.response(path, T0 + 1500, "r2", out=999)
        archived = self.rollout("t1", folder="archived_sessions")  # an archived copy of the same thread
        self.response(archived, T0 + 1200, "r9", out=50)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        line = self.codex_note(sha)["gpt-5.6-sol"]
        self.assertEqual((line["in"], line["out"]), ("1000", "10"))

    def test_cost_is_per_response_with_long_context_rates(self):
        path = self.rollout("t1")
        self.commit(1)
        self.response(path, T0 + 1100, "r1", input=200_000, out=1000)
        self.response(path, T0 + 1200, "r2", input=200_000, out=1000)
        short = self.commit(2)
        self.response(path, T0 + 2100, "r3", input=300_000, out=1000)
        long = self.commit(3)
        tool.backfill(EMAIL, quiet=True)
        # gpt-5.6-sol: $4 input and $20 output per 1M up to 272K input, $8 and $30 above.
        self.assertAlmostEqual(float(self.codex_note(short)["gpt-5.6-sol"]["cost_usd"]), 2 * (0.8 + 0.02), places=4)
        self.assertAlmostEqual(float(self.codex_note(long)["gpt-5.6-sol"]["cost_usd"]), 2.4 + 0.03, places=4)

    def test_input_includes_the_cached_and_cache_write_tokens(self):
        path = self.rollout("t1")
        self.commit(1)
        self.response(path, T0 + 1100, "r1", input=10_000, cached=3000, written=2000, out=1000)
        short = self.commit(2)
        # 300K input, of which only 100K is not cached: the request is still above 272K.
        self.response(path, T0 + 2100, "r2", input=300_000, cached=200_000, out=1000)
        long = self.commit(3)
        tool.backfill(EMAIL, quiet=True)
        line = self.codex_note(short)["gpt-5.6-sol"]
        self.assertEqual((line["in"], line["cache_read"], line["cache_write"]), ("5000", "3000", "2000"))
        # gpt-5.6-sol up to 272K: $4 input, $0.40 cached, $5 cache write and $20 output per 1M.
        self.assertAlmostEqual(float(line["cost_usd"]), 0.02 + 0.0012 + 0.01 + 0.02, places=4)
        # Above 272K: $8 input, $0.80 cached and $30 output per 1M.
        self.assertAlmostEqual(float(self.codex_note(long)["gpt-5.6-sol"]["cost_usd"]), 0.8 + 0.16 + 0.03, places=4)

    def test_cost_uses_the_processing_mode(self):
        path = self.rollout("t1")
        self.commit(1)
        settings = {"model": "gpt-5.6-sol", "cwd": str(self.repo), "service_tier": "fast"}
        self.codex_line(path, T0 + 1050, "event_msg", {"type": "thread_settings_applied", "thread_settings": settings})
        self.response(path, T0 + 1100, "r1", input=1000, out=1000)
        fast = self.commit(2)
        settings["service_tier"] = "ultrafast"  # no Ultrafast price for this model
        self.codex_line(path, T0 + 2050, "event_msg", {"type": "thread_settings_applied", "thread_settings": settings})
        self.response(path, T0 + 2100, "r2", input=1000, out=1000)
        unpriced = self.commit(3)
        tool.backfill(EMAIL, quiet=True)
        self.assertAlmostEqual(float(self.codex_note(fast)["gpt-5.6-sol"]["cost_usd"]), 0.008 + 0.04, places=4)
        self.assertEqual(self.codex_note(unpriced)["gpt-5.6-sol"]["cost_usd"], "0.0000")

    def test_token_count_is_the_fallback_for_old_files(self):
        path = self.rollout("t1", model="gpt-5.3-codex")
        self.commit(1)
        last = {"input_tokens": 100_000, "cached_input_tokens": 0, "output_tokens": 10_000}
        info = {"total_token_usage": {"total_tokens": 110_000}, "last_token_usage": last}
        self.codex_line(path, T0 + 1100, "event_msg", {"type": "token_count", "info": info})
        self.codex_line(path, T0 + 1200, "event_msg", {"type": "token_count", "info": info})  # only rate limits changed
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        line = self.codex_note(sha)["gpt-5.3-codex"]
        self.assertEqual(line["out"], "10000")
        self.assertAlmostEqual(float(line["cost_usd"]), 0.175 + 0.14, places=4)

    def test_claude_and_codex_in_one_note(self):
        transcript = self.transcript()
        rollout = self.rollout("t1")
        self.commit(1)
        self.call(transcript, T0 + 1100, out=20)
        self.response(rollout, T0 + 1200, "r1", out=30)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual([entry["_tool"] for entry in self.note(sha)], ["claude-code", "codex"])
        _, per_model, total = self.totals(sha)
        self.assertEqual(total.tokens["out"], 50)
        self.assertEqual({model: t.tool for model, t in per_model.items()}, {"claude-opus-4-1": "claude-code", "gpt-5.6-sol": "codex"})


class HookTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.commit(0)
        tool.install()

    def test_post_commit_writes_the_note(self):
        path = self.transcript()
        self.call(path, T0 + 500, out=33)
        sha = self.commit(1)
        self.assertEqual(self.note(sha)[0]["out"], "33")

    def test_amend_keeps_one_copy(self):
        path = self.transcript()
        self.call(path, T0 + 500, out=33)
        self.commit(1)
        amended = self.commit(1, message="commit 1 amended", amend=True)
        _, _, total = self.totals(amended)
        self.assertEqual(total.tokens["out"], 33)

    def test_squash_keeps_both_windows(self):
        path = self.transcript()
        self.call(path, T0 + 500, out=10)
        self.commit(1)
        self.call(path, T0 + 1500, out=20)
        self.commit(2)
        self.git("rebase", "-q", "-i", "HEAD~2", env={"GIT_SEQUENCE_EDITOR": "sed -i -e '2s/^pick/squash/'", "GIT_EDITOR": "true"})
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2\n")
        _, _, total = self.totals("HEAD")
        self.assertEqual(total.tokens["out"], 30)

    def test_install_keeps_the_existing_hook_and_is_idempotent(self):
        hook = Path(self.git("rev-parse", "--git-path", "hooks").strip()) / "post-commit"
        hook.write_text("#!/bin/sh\necho existing > marker.txt\nexit 0\n")
        tool.install()
        tool.install()
        content = hook.read_text()
        self.assertEqual(content.count(tool.HOOK_START), 1)
        self.assertIn("echo existing", content)
        path = self.transcript()
        self.call(path, T0 + 500, out=33)
        sha = self.commit(1)
        self.assertTrue((self.repo / "marker.txt").exists())
        self.assertEqual(self.note(sha)[0]["out"], "33")

    def test_codex_fork_counts_the_copied_parent_records_once(self):
        parent = self.rollout("parent")
        self.response(parent, T0 + 500, "r1", out=10)
        self.commit(1)
        os.utime(parent, (T0 + 600, T0 + 600))  # the parent did not change after commit 1
        fork = self.rollout("fork", forked_from="parent")
        self.response(fork, T0 + 1500, "r1", out=10)  # copied from the parent
        self.response(fork, T0 + 1600, "r2", out=5)
        sha = self.commit(2)
        self.assertEqual(self.codex_note(sha)["gpt-5.6-sol"]["out"], "5")

    def test_hook_never_fails_the_commit(self):
        path = self.transcript()
        path.write_text("{not json\n")
        sha = self.commit(1)
        self.assertEqual(self.note(sha), [])

    def test_range_sums_the_branch(self):
        path = self.transcript()
        self.git("branch", "base")
        self.call(path, T0 + 500, out=10)
        self.commit(1)
        self.call(path, T0 + 1500, out=20, model="claude-sonnet-4-5")
        self.commit(2)
        per_commit, per_model, total = self.totals("base..HEAD")
        self.assertEqual(len(per_commit), 2)
        self.assertEqual(total.tokens["out"], 30)
        self.assertEqual(set(per_model), {"claude-opus-4-1", "claude-sonnet-4-5"})


class RemoteTest(Sandbox):
    def test_push_merges_notes_from_two_clones(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        path = self.transcript()
        self.commit(0)
        self.git("push", "-q", "origin", "HEAD:main")
        other = self.tmp / "other"
        self.git("clone", "-q", str(remote), str(other))

        self.call(path, T0 + 500, out=10)
        here = self.commit(1)
        tool.backfill(EMAIL, quiet=True)
        self.git("push", "-q", "origin", "HEAD:main")
        tool.push("origin")

        os.chdir(other)
        tool.write_note(self.git("rev-parse", "HEAD").strip(), "claude-code model=x out=5 from=1 to=2\n")
        tool.push("origin")  # not a fast-forward: needs the merge
        self.git("fetch", "-q", "origin", "main")
        tool.fetch("origin")
        self.assertEqual(tool.parse_note(tool.read_note(here))[0]["out"], "10")


if __name__ == "__main__":
    unittest.main()
