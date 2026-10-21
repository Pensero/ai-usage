"""End-to-end tests: real git repositories and fake Claude Code and Codex transcripts in a temporary HOME."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
            "GIT_AI_USAGE_OFFLINE": "1",
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
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

    def commit_file(self, n: int, name: str, content: str, message: str = None) -> str:
        """A commit at T0 + n * 1000 that writes `content` to `name`."""
        stamp = f"@{T0 + n * 1000} +0000"
        (self.repo / name).write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message or f"commit {n}", env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        return self.git("rev-parse", "HEAD").strip()

    def patch(self, path: Path, when: float, name: str, added: list, deleted: list = ()):
        """A Claude Code Edit result on `name` with a structured patch."""
        self.messages += 1
        lines = [f"+{line}" for line in added] + [f"-{line}" for line in deleted]
        record = {"type": "user", "timestamp": iso(when), "cwd": str(self.repo), "uuid": f"e{self.messages}",
                  "toolUseResult": {"filePath": str(self.repo / name), "structuredPatch": [{"lines": lines}]}}
        with path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def edited(self, sha: str, tool_name: str = "claude-code") -> dict:
        return next(entry for entry in self.note(sha) if entry["_tool"] == tool_name and "model" not in entry)

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

    def test_cost_uses_the_published_rates(self):
        # USD per 1M tokens from platform.claude.com/docs/en/about-claude/pricing:
        # (input, 5m cache write, 1h cache write, cache hit, output).
        cases = [
            ("claude-sonnet-5", False, 0, (2, 2.5, 4, 0.2, 10)),
            ("claude-sonnet-5-5", False, 0, (2, 2.5, 4, 0.1, 10)),
            ("claude-opus-5-5", False, 0, (4, 5, 8, 0.2, 20)),
            ("claude-opus-5-5", True, 0, (8, 10, 16, 0.4, 40)),
            ("claude-opus-5", True, 0, (10, 12.5, 20, 1, 50)),
            ("claude-fable-5-1", False, 0, (10, 12.5, 20, 0.25, 50)),
            ("claude-mythos-5-1", False, 0, (10, 12.5, 20, 0.25, 50)),
            ("claude-mythos-5", False, 0, (10, 12.5, 20, 1, 50)),
            ("claude-haiku-5-5", False, 0, (0.1, 0.125, 0.2, 0.01, 0.5)),
            # Haiku 5.5 above 100K prompt tokens, cache reads included: 1,000 + 99,500 + 2,000.
            ("claude-haiku-5-5", False, 99_500, (0.5, 0.625, 1, 0.05, 2.5)),
        ]
        for model, fast, cached, (input_rate, write, write_1h, hit, output) in cases:
            with self.subTest(model=model, fast=fast, cached=cached):
                tokens = {"in": 1000, "out": 1000, "cache_read": cached or 1000, "cache_write": 2000,
                          "cache_write_1h": 1000, "web_search": 0}
                expected = (1000 * input_rate + 1000 * output + (cached or 1000) * hit + 1000 * write
                            + 1000 * write_1h) / 1_000_000
                self.assertAlmostEqual(tool.cost_usd(tool.Call(T0, model, fast, tokens)), expected, places=9)

    def test_each_line_names_the_commit_author(self):
        path = self.transcript()
        self.commit(1)
        self.call(path, T0 + 1500, out=100)
        self.edit(path, T0 + 1600, lines=3)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual([entry["author"] for entry in self.note(sha)], [EMAIL, EMAIL])

    def test_a_rewritten_line_replaces_its_old_copy(self):
        old = "claude-code model=m out=10 cost_usd=1.0000 from=1 to=2\nclaude-code added=4 deleted=0 from=1 to=2\n"
        new = f"claude-code model=m out=10 cost_usd=1.5000 author={EMAIL} from=1 to=2\nclaude-code added=4 deleted=0 author={EMAIL} from=1 to=2\n"
        _, per_model, total = tool.summarize([(None, old + new)])
        self.assertEqual((total.tokens["out"], total.added), (10, 4))
        self.assertAlmostEqual(total.cost, 1.5)
        self.assertEqual(list(per_model), [("claude-code", "m")])

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


    def test_committed_lines_follow_the_commit_that_holds_them(self):
        path = self.transcript()
        self.commit(1)
        self.patch(path, T0 + 1500, "a.py", ["alpha = 1", "beta = 2"])
        self.patch(path, T0 + 1600, "b.py", ["gamma = 3"], deleted=["old"])
        second = self.commit_file(2, "a.py", "alpha = 1\nbeta = 2\n")
        third = self.commit_file(3, "b.py", "gamma = 3\n")  # no edit in its window: the lines came earlier
        self.git("checkout", "-q", "-b", "copy", "HEAD~1")
        copy = self.commit_file(4, "b.py", "gamma = 3\n", message="copy of commit 3")

        self.assertEqual(tool.backfill(EMAIL, quiet=True), 2)

        self.assertEqual((self.edited(second)["added"], self.edited(second)["committed_added"]), ("3", "2"))
        self.assertEqual((self.edited(third)["added"], self.edited(third)["committed_added"]), ("0", "1"))
        self.assertEqual(self.edited(third)["committed_deleted"], "0")
        self.assertEqual(tool.read_note(copy), "")  # the same lines again: not new work

    def test_a_hand_changed_line_and_an_old_line_are_not_committed_lines(self):
        path = self.transcript()
        self.commit(1)
        self.patch(path, T0 + 1500, "a.py", ["alpha = 1", "beta = 2"])
        second = self.commit_file(2, "a.py", "alpha = 1\nbeta = 3\n")
        self.patch(path, T0 + 2500 - tool.COMMITTED_LOOKBACK - 1, "c.py", ["too old"])
        self.commit_file(3, "c.py", "too old\n")

        tool.backfill(EMAIL, quiet=True)

        self.assertEqual(self.edited(second)["committed_added"], "1")
        self.assertEqual(tool.read_note(self.git("rev-parse", "HEAD").strip()), "")


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
        self.assertEqual(set(per_model), {("claude-code", "claude-opus-4-1"), ("codex", "gpt-5.6-sol")})

    def test_a_model_name_in_both_tools_stays_apart(self):
        note = "claude-code model=shared out=20 from=1 to=2\ncodex model=shared out=30 from=1 to=2\n"
        _, per_model, _ = tool.summarize([(None, note)])
        self.assertEqual({key: t.tokens["out"] for key, t in per_model.items()}, {("claude-code", "shared"): 20, ("codex", "shared"): 30})

    def legacy_count(self, path: Path, when: float, total: int, out: int):
        last = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": out}
        info = {"total_token_usage": {"total_tokens": total}, "last_token_usage": last}
        self.codex_line(path, when, "event_msg", {"type": "token_count", "info": info})

    def test_a_file_resumed_by_a_newer_codex_keeps_its_old_turns(self):
        path = self.rollout("t1")
        self.commit(1)
        self.legacy_count(path, T0 + 1100, total=1100, out=100)
        self.codex_line(path, T0 + 1200, "turn_context", {"cwd": str(self.repo), "model": "gpt-5.6-sol"})
        self.response(path, T0 + 1300, "r1", out=50)
        self.legacy_count(path, T0 + 1300, total=2150, out=50)  # the same response as r1
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual(self.codex_note(sha)["gpt-5.6-sol"]["out"], "150")

    def test_old_sessions_that_start_the_same_way_both_count(self):
        first, second = self.rollout("t1"), self.rollout("t2")
        self.commit(1)
        self.legacy_count(first, T0 + 1100, total=1100, out=100)
        self.legacy_count(second, T0 + 1200, total=1100, out=100)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual(self.codex_note(sha)["gpt-5.6-sol"]["out"], "200")

    def test_an_old_fork_counts_the_copied_parent_responses_once(self):
        parent = self.rollout("parent")
        self.commit(1)
        self.legacy_count(parent, T0 + 1100, total=1100, out=100)
        fork = self.rollout("fork", forked_from="parent")
        self.legacy_count(fork, T0 + 1100, total=1100, out=100)  # copied from the parent
        self.legacy_count(fork, T0 + 1200, total=2130, out=30)
        sha = self.commit(2)
        tool.backfill(EMAIL, quiet=True)
        self.assertEqual(self.codex_note(sha)["gpt-5.6-sol"]["out"], "130")


    def test_codex_committed_lines(self):
        path = self.rollout("t1")
        self.commit(1)
        self.response(path, T0 + 1500, "r1", out=100)
        patch = {"type": "item_completed", "item": {"type": "FileChange", "id": "p1", "status": "completed", "changes": {
            str(self.repo / "a.py"): {"type": "add", "content": "a = 1\nb = 2\n"},
            str(self.repo / "b.py"): {"type": "update", "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n"},
        }}}
        self.codex_line(path, T0 + 1600, "event_msg", patch)
        second = self.commit_file(2, "a.py", "a = 1\nb = 2\nc = 3\n")

        tool.backfill(EMAIL, quiet=True)

        edited = self.edited(second, "codex")
        self.assertEqual((edited["added"], edited["committed_added"], edited["committed_deleted"]), ("3", "2", "0"))


class HookTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.commit(0)
        tool.install()

    def test_post_commit_writes_the_committed_lines(self):
        path = self.transcript()
        self.call(path, T0 + 500, out=33)
        self.patch(path, T0 + 600, "a.py", ["alpha = 1", "beta = 2"])
        sha = self.commit_file(1, "a.py", "alpha = 1\n")
        edited = self.edited(sha)
        self.assertEqual((edited["added"], edited["committed_added"]), ("2", "1"))

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
        self.assertEqual(set(per_model), {("claude-code", "claude-opus-4-1"), ("claude-code", "claude-sonnet-4-5")})


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

    def test_forced_backfill_after_a_push_counts_once(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        path = self.transcript()
        self.commit(0)
        self.call(path, T0 + 500, out=10)
        sha = self.commit(1)
        tool.write_note(sha, f"claude-code model=claude-opus-4-1 out=10 from={T0} to={T0 + 1000}\n")
        self.git("push", "-q", "origin", "HEAD:main")
        tool.push("origin")

        self.git("update-ref", "-d", tool.NOTES_REF)
        tool.backfill(EMAIL, quiet=True)
        tool.push("origin")

        self.assertEqual(len(self.note(sha)), 2)
        _, _, total = self.totals(sha)
        self.assertEqual(total.tokens["out"], 10)


class PriceSourceTest(Sandbox):
    def setUp(self):
        super().setUp()
        del os.environ["GIT_AI_USAGE_OFFLINE"]
        self.litellm = self.tmp / "litellm.json"
        self.models_dev = self.tmp / "models-dev.json"
        os.environ["GIT_AI_USAGE_LITELLM_URL"] = self.litellm.as_uri()
        os.environ["GIT_AI_USAGE_MODELS_DEV_URL"] = self.models_dev.as_uri()
        self.write_sources(litellm_input=3)

    def write_sources(self, litellm_input: float):
        usd = 1e-6
        self.litellm.write_text(json.dumps({
            "gpt-6-sol": {"litellm_provider": "openai", "input_cost_per_token": litellm_input * usd,
                          "output_cost_per_token": 30 * usd, "cache_read_input_token_cost": 0.3 * usd,
                          "input_cost_per_token_priority": 6 * usd, "output_cost_per_token_priority": 60 * usd,
                          "input_cost_per_token_above_272k_tokens_priority": 12 * usd,
                          "output_cost_per_token_above_272k_tokens_priority": 90 * usd},
            "claude-opus-5": {"litellm_provider": "anthropic", "input_cost_per_token": 6 * usd,
                              "output_cost_per_token": 30 * usd, "provider_specific_entry": {"fast": 2}},
        }))
        self.models_dev.write_text(json.dumps({"openai": {"models": {
            "gpt-6-sol": {"cost": {"input": 9, "output": 9, "tiers": [
                {"input": 5, "output": 40, "cache_read": 0.5, "tier": {"type": "context", "size": 272000}}]}},
            "gpt-9": {"cost": {"input": 1, "output": 2}},
        }}}))

    def refresh(self) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tool.refresh_prices()
        return stderr.getvalue()

    def codex_call(self, model="gpt-6-sol", tokens_in=1000, tier="standard", when=T0):
        tokens = {"in": tokens_in, "out": 1000, "cache_read": 0, "cache_write": 0}
        return tool.Call(when, model, False, tokens, tool=tool.CODEX_TOOL, tier=tier)

    def codex(self, *args, **kwargs):
        return tool.priced(self.codex_call(*args, **kwargs))

    def claude(self, fast: bool):
        tokens = {"in": 1000, "out": 1000, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0, "web_search": 0}
        return tool.priced(tool.Call(T0, "claude-opus-5-20261001", fast, tokens))

    def test_litellm_first_then_models_dev_then_builtin(self):
        self.refresh()
        cost, source = self.codex()
        self.assertEqual(source, "litellm+models.dev")
        self.assertAlmostEqual(cost, (1000 * 3 + 1000 * 30) / 1e6)
        # LiteLLM has no long-context rates here, so models.dev's apply: 300K input at $5, output at $40.
        self.assertAlmostEqual(self.codex(tokens_in=300_000)[0], (300_000 * 5 + 1000 * 40) / 1e6)
        cost, source = self.codex(tier="priority")
        self.assertEqual(source, "litellm")
        self.assertAlmostEqual(cost, (1000 * 6 + 1000 * 60) / 1e6)
        self.assertAlmostEqual(self.codex(tokens_in=300_000, tier="priority")[0], (300_000 * 12 + 1000 * 90) / 1e6)
        # Only models.dev has gpt-9.
        cost, source = self.codex("gpt-9")
        self.assertEqual(source, "models.dev")
        self.assertAlmostEqual(cost, (1000 * 1 + 1000 * 2) / 1e6)
        # In neither source: the built-in Ultrafast rates of gpt-6.1-sol, $12 / $60.
        cost, source = self.codex("gpt-6.1-sol", tier="ultrafast")
        self.assertEqual(source, "builtin")
        self.assertAlmostEqual(cost, (1000 * 12 + 1000 * 60) / 1e6)

    def test_claude_fast_uses_the_litellm_multiplier(self):
        self.refresh()
        self.assertAlmostEqual(self.claude(fast=False)[0], (1000 * 6 + 1000 * 30) / 1e6)
        cost, source = self.claude(fast=True)
        self.assertEqual(source, "litellm")
        self.assertAlmostEqual(cost, (1000 * 12 + 1000 * 60) / 1e6)

    def test_a_failed_download_keeps_the_builtin_prices(self):
        self.litellm.unlink()
        self.models_dev.unlink()
        self.assertIn("could not download the litellm prices", self.refresh())
        cost, source = self.codex()
        self.assertEqual(source, "builtin")
        self.assertAlmostEqual(cost, (1000 * 2 + 1000 * 10) / 1e6)

    def test_downloads_at_most_once_a_day(self):
        self.refresh()
        self.write_sources(litellm_input=4)
        self.refresh()
        self.assertAlmostEqual(self.codex()[0], (1000 * 3 + 1000 * 30) / 1e6)
        day_old = time.time() - tool.PRICE_CACHE_SECONDS - 1
        os.utime(tool.price_cache_path(), (day_old, day_old))
        self.refresh()
        self.assertAlmostEqual(self.codex()[0], (1000 * 4 + 1000 * 30) / 1e6)

    def test_the_hook_and_offline_never_download(self):
        self.commit(1)
        with contextlib.redirect_stdout(io.StringIO()):
            tool.main(["hook", "post-commit"])
            tool.main(["--offline", "backfill"])
        self.assertFalse(tool.price_cache_path().exists())
        del os.environ["GIT_AI_USAGE_OFFLINE"]
        self.refresh()
        self.assertEqual(self.codex()[1], "litellm+models.dev")
        os.environ["GIT_AI_USAGE_OFFLINE"] = "1"
        self.assertEqual(self.codex()[1], "builtin")

    def test_builtin_prices_cover_the_gpt_5_models(self):
        cost, source = self.codex("gpt-5.5-2026-04-23")
        self.assertEqual(source, "builtin")
        self.assertAlmostEqual(cost, (1000 * 5 + 1000 * 30) / 1e6)
        self.assertAlmostEqual(self.codex("gpt-5.4", tokens_in=300_000)[0], (300_000 * 5 + 1000 * 22.5) / 1e6)
        # The pro models have no cached rate, and a longer version is not priced as its prefix.
        self.assertAlmostEqual(self.codex("gpt-5.5-pro")[0], (1000 * 30 + 1000 * 180) / 1e6)
        self.assertEqual(self.codex("gpt-5.9")[1], "none")

    def test_the_note_names_the_price_sources(self):
        self.refresh()
        tiers = ("standard", "priority", "flex")
        calls = [self.codex_call(tier=tier, when=T0 + index) for index, tier in enumerate(tiers, 1)]
        note = tool.note_for_window(T0, T0 + 10, calls, [])
        self.assertIn(" price_src=builtin+litellm+models.dev ", note)


if __name__ == "__main__":
    unittest.main()
