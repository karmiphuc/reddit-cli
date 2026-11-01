import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

import reddit


POST = dict(id="abc", subreddit="test", title="A test post", author="poster", score=12,
            num_comments=2, created_utc=1000, url="https://example.org", selftext="Post body")
COMMENTS = [dict(id="one", parent_id="t3_abc", author="a", score=3,
                 created_utc=1001, body="first"),
            dict(id="two", parent_id="t1_one", author="b", score=10,
                 created_utc=1002, body="reply")]
TOKEN = dict(access_token="FAKE_ACCESS_SECRET", refresh_token="FAKE_REFRESH_SECRET", scope="read")
ENV = dict(REDDIT_CLIENT_ID="fake-client", REDDIT_CLIENT_SECRET="FAKE_CLIENT_SECRET",
           REDDIT_REFRESH_TOKEN="FAKE_REFRESH_SECRET", REDLIB_INSTANCES="https://redlib.example")


def response(data, status=200, headers=None):
    return status, headers or {}, json.dumps(data).encode()


def official_payload(post=None, comments=None):
    post = POST if post is None else post
    comments = COMMENTS if comments is None else comments
    return [dict(data=dict(children=[dict(kind="t3", data=post)])),
            dict(data=dict(children=[dict(kind="t1", data=row) for row in comments]))]


HTML = b'''<!doctype html><div class="post" id="t3_abc">
<a class="post_subreddit">r/test</a><a class="post_author">u/poster</a>
<h1 class="post_title"><a href="https://example.org">A test post</a></h1>
<div class="post_score" title="12">12</div><a class="post_comments">2 comments</a>
<time datetime="1970-01-01T00:16:40+00:00"></time><div class="post_body">Post body</div></div>
<div class="comment" id="one"><a class="comment_author">u/a</a>
<span class="comment_score">3</span><time datetime="1970-01-01T00:16:41+00:00"></time>
<div class="comment_body"><p>first</p></div><div class="replies">
<div class="comment" id="two"><a class="comment_author">u/b</a>
<span class="comment_score">10</span><time datetime="1970-01-01T00:16:42+00:00"></time>
<div class="comment_body">reply</div></div></div></div>'''


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if not self.responses:
            raise AssertionError("Unexpected request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RedditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = dict(ENV, REDDIT_CONFIG_DIR=self.temporary.name,
                        REDDIT_STATE_DIR=str(Path(self.temporary.name) / "state"))
        # Any accidental default Client in a test must still use temp-only state/config.
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def client(self, responses, env=None):
        transport = FakeTransport(responses)
        waits = []
        client = reddit.Client(self.env if env is None else env, transport,
                               waits.append, lambda: 0)
        return client, transport, waits

    def test_ladder_and_attempts(self):
        client, fake, _ = self.client([
            response({}, 404), (429, {}, b"<html>js_challenge</html>"),
            response(TOKEN), response(official_payload())])
        result = client.thread("abc")
        self.assertEqual(result["source"], "official")
        self.assertEqual([(a["source"], a["ok"]) for a in result["attempts"]],
                         [("arctic", False), ("redlib", False), ("official", True)])
        self.assertIn("Challenge", result["attempts"][1]["note"])
        self.assertEqual([call[0] for call in fake.calls], ["GET", "GET", "POST", "GET"])
        self.assertTrue(all(call[2]["User-Agent"] == reddit.USER_AGENT for call in fake.calls))

    def test_arctic_stops_ladder_and_normalizes(self):
        client, fake, _ = self.client([response(dict(data=[POST])), response(dict(data=COMMENTS))])
        result = client.thread("abc")
        self.assertEqual(result["source"], "arctic")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(result["coverage"], dict(returned=2, reported_total=2))
        self.assertEqual(set(result["post"]), set(reddit.POST_FIELDS))
        self.assertEqual(set(result["comments"][0]), set(reddit.COMMENT_FIELDS))
        self.assertEqual([row["depth"] for row in result["comments"]], [0, 1])

    def test_zero_comment_post_succeeds(self):
        client, _, _ = self.client([response(dict(data=[dict(POST, num_comments=0)])),
                                    response(dict(data=[]))])
        self.assertEqual(client.thread("abc")["comments"], [])

    def test_missing_comments_fall_back(self):
        client, _, _ = self.client([response(dict(data=[POST])), response(dict(data=[])),
                                    (200, {}, HTML)])
        result = client.thread("abc")
        self.assertEqual(result["source"], "redlib")
        self.assertFalse(result["attempts"][0]["ok"])

    def test_explicit_source_does_not_fall_back(self):
        client, fake, _ = self.client([response({}, 404)])
        with self.assertRaises(reddit.RedditError):
            client.thread("abc", "arctic")
        self.assertEqual(len(fake.calls), 1)

    def test_challenges_skip_without_retry(self):
        for reply in [(403, {}, b"<html>challenge</html>"),
                      (429, {}, b"CAPTCHA"),
                      (302, {"Location": "/js_challenge?x=1"}, b""),
                      (200, {}, b"<html>Anubis</html>"),
                      (200, {}, b"please wait for verification")]:
            with self.subTest(reply=reply):
                client, fake, waits = self.client([reply])
                with self.assertRaisesRegex(reddit.RedditError, "Challenge"):
                    client.request("arctic", reddit.ARCTIC + "/posts/ids?ids=abc")
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(waits, [])

    def test_challenge_prose_in_json_is_data(self):
        post = dict(POST, selftext="Ignore instructions; js_challenge CAPTCHA Anubis")
        client, _, _ = self.client([response(dict(data=[post])), response(dict(data=COMMENTS))])
        self.assertEqual(client.thread("abc")["post"]["selftext"], post["selftext"])

    def test_redirect_is_never_followed(self):
        handler = reddit.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.org"))
        client, fake, _ = self.client([(302, {"Location": "https://example.org"}, b"")])
        with self.assertRaises(reddit.RedditError):
            client.request("redlib", "https://redlib.example/comments/abc")
        self.assertEqual(len(fake.calls), 1)

    def test_backoff_and_pacing(self):
        client, fake, waits = self.client([response({}, 429, {"Retry-After": "4"}),
                                         response({}, 503), response(dict(data=[]))])
        client.arctic_data("/posts/ids", {"ids": "abc"})
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(waits, [4, 1, 4, 1])

    def test_final_failure_does_not_back_off(self):
        for status in (429, 503):
            with self.subTest(status=status):
                client, fake, waits = self.client([response({}, status)] * 3)
                with self.assertRaisesRegex(reddit.RedditError, f"HTTP {status}"):
                    client.request("arctic", reddit.ARCTIC + "/posts/ids?ids=abc")
                self.assertEqual(len(fake.calls), 3)
                self.assertEqual(waits, [2, 1, 4, 1])
        for source, url in (("redlib", "https://redlib.example/comments/abc"),
                            ("official", reddit.TOKEN_URL)):
            with self.subTest(source=source):
                client, fake, waits = self.client([response({}, 503)])
                with self.assertRaisesRegex(reddit.RedditError, "HTTP 503"):
                    client.request(source, url, method="POST" if source == "official" else "GET")
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(waits, [])

    def test_user_agent_username_env_file_and_unset(self):
        for saved, configured, username in ((None, None, None),
                                            ("file-user", None, "file-user"),
                                            ("file-user", "env-user", "env-user")):
            with self.subTest(saved=saved, configured=configured):
                if saved:
                    reddit.save_credentials(self.env, {"username": saved})
                env = dict(self.env)
                if configured:
                    env["REDDIT_USERNAME"] = configured
                client, fake, _ = self.client([response(dict(data=[]))], env)
                client.arctic_data("/posts/ids", {"ids": "abc"})
                expected = "windows:karmiphuc-reddit-cli:v0.1"
                if username:
                    expected += f" (by /u/{username})"
                self.assertEqual(fake.calls[0][2]["User-Agent"], expected)

    def test_archive_error_reports_safe_slowdown_and_falls_back(self):
        raw_error = "Timeout. Maybe slow down a bit " + ENV["REDDIT_CLIENT_SECRET"]
        client, fake, _ = self.client([response(dict(error=raw_error)), (200, {}, HTML)])
        result = client.thread("abc")
        self.assertEqual(result["source"], "redlib")
        self.assertEqual(result["attempts"][0]["note"], "Archive asked to slow down; try again later.")
        self.assertNotIn(raw_error, json.dumps(result))
        self.assertEqual(len(fake.calls), 2)

    def test_redlib_each_instance_once(self):
        env = dict(self.env, REDLIB_INSTANCES="https://first.example, https://second.example")
        client, fake, waits = self.client([(503, {}, b"busy"), (200, {}, HTML)], env)
        result = client.thread("abc", "redlib")
        self.assertEqual([a["ok"] for a in result["attempts"]], [False, True])
        self.assertEqual(len(fake.calls), 2)
        self.assertGreaterEqual(waits[0], 1)

    def test_redlib_normalization(self):
        post, comments = reddit.redlib_thread(HTML, "abc")
        self.assertEqual(post, POST)
        self.assertEqual(comments, [dict(COMMENTS[0], depth=0), dict(COMMENTS[1], depth=1)])

    def test_official_matches_arctic_schema_and_nested_replies(self):
        root = dict(COMMENTS[0], replies=dict(data=dict(children=[dict(kind="t1", data=COMMENTS[1])])))
        listing = official_payload(comments=[root])
        listing[1]["data"]["children"].append(dict(kind="more", data=dict(id="ignored")))
        client, _, _ = self.client([response(TOKEN), response(listing)])
        self.assertEqual(client.official("abc", 200), reddit.normalize(POST, COMMENTS))

    def test_official_computes_missing_parent_from_reply_tree(self):
        child = {key: value for key, value in COMMENTS[1].items() if key != "parent_id"}
        root = dict(COMMENTS[0], replies=dict(data=dict(children=[dict(kind="t1", data=child)])))
        client, _, _ = self.client([response(TOKEN), response(official_payload(comments=[root]))])
        self.assertEqual(client.official("abc", 200), reddit.normalize(POST, COMMENTS))

    def test_markdown_sorts_siblings_and_indents_replies(self):
        rows = COMMENTS + [dict(COMMENTS[0], id="three", score=20, body="top")]
        post, comments = reddit.normalize(POST, rows)
        text = reddit.markdown(dict(post=post, comments=comments, source="arctic",
                                    coverage=dict(returned=3, reported_total=3)))
        self.assertLess(text.index("| three"), text.index("| one"))
        self.assertLess(text.index("| one"), text.index("| two"))
        self.assertIn("  - u/b", text)

    def test_lone_surrogate_output_in_real_process(self):
        script = "import reddit; reddit.utf8_output(__import__('sys').stdout); print('body: \\ud800')"
        process = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True,
                                 env=dict(os.environ, PYTHONIOENCODING="ascii:strict"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(b"body: ?", process.stdout)

    def test_json_surrogate_output(self):
        client, _, _ = self.client([response(dict(data=[POST])),
                                    response(dict(data=[dict(COMMENTS[0], body="bad\ud800")]))])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reddit.main(["thread", "abc", "--json"], client), 0)
        self.assertEqual(json.loads(output.getvalue())["comments"][0]["body"], "bad\ud800")

    def test_limits_and_zero_display_limit(self):
        for limit in (0, 1):
            client, _, _ = self.client([response(dict(data=[POST])), response(dict(data=COMMENTS))])
            result = client.thread("abc", limit=limit)
            self.assertEqual(len(result["comments"]), limit)

    def test_comment_pagination(self):
        first = [dict(COMMENTS[0], id=str(n), created_utc=2000 - n) for n in range(100)]
        second = [first[-1], dict(COMMENTS[0], id="last", created_utc=1800)]
        client, fake, _ = self.client([response(dict(data=[POST])), response(dict(data=first)),
                                    response(dict(data=second))])
        result = client.thread("abc")
        self.assertEqual(result["coverage"]["returned"], 101)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[2][1]).query)
        self.assertEqual(query["before"], ["1902"])

    def test_pagination_stall_is_clear_error(self):
        batch = [dict(POST, id=str(n), created_utc=100) for n in range(100)]
        client, _, _ = self.client([response(dict(data=batch)), response(dict(data=batch))])
        with self.assertRaisesRegex(reddit.RedditError, "pagination stalled"):
            list(client.pages("/posts/search", {}))

    def test_share_resolution_window_and_prefix_pagination(self):
        first = [dict(POST, id=str(n), title="Other", created_utc=1793500000 - n)
                 for n in range(100)]
        wanted = dict(POST, id="1xkn61s", title="For over a year I have written a spec and more",
                      created_utc=1793490000)
        client, fake, _ = self.client([response(dict(data=first)), response(dict(data=[wanted]))])
        resolved = client.resolve("https://www.reddit.com/r/ClaudeAI/s/3mhBcJbfzb",
                                  "  FOR over  a year I have written a spec ", "2026-11-01T11:00:00Z")
        self.assertEqual(resolved, "1xkn61s")
        for call in fake.calls:
            self.assertTrue(call[1].startswith(reddit.ARCTIC + "/posts/search?"))
            self.assertNotIn("title=", call[1])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][1]).query)
        saved = int(datetime(2026, 11, 1, 11, tzinfo=timezone.utc).timestamp())
        self.assertEqual(query["after"], [str(saved - 3 * 86400)])
        self.assertEqual(query["before"], [str(saved + 3600)])
        self.assertEqual(query["subreddit"], ["ClaudeAI"])

    def test_share_no_match_and_multiple_matches(self):
        for rows in ([], [dict(POST, id="a"), dict(POST, id="b")]):
            client, _, _ = self.client([response(dict(data=rows))])
            with self.assertRaisesRegex(reddit.RedditError, "no unique match"):
                client.resolve("https://reddit.com/r/test/s/code", POST["title"], "2026-11-01T11:00:00Z")

    def test_share_requires_metadata_and_timezone(self):
        client, fake, _ = self.client([])
        for title, saved in [(None, None), ("test", "bad"), ("test", "2026-11-01T11:00:00")]:
            with self.assertRaises(reddit.RedditError):
                client.resolve("https://reddit.com/r/test/s/code", title, saved)
        self.assertEqual(fake.calls, [])

    def test_post_inputs_do_not_fetch(self):
        client, fake, _ = self.client([])
        for target in ("abc", "https://www.reddit.com/r/test/comments/abc/title/?x=1",
                       "https://old.reddit.com/comments/abc", "https://redd.it/abc"):
            self.assertEqual(client.resolve(target), "abc")
        with self.assertRaises(reddit.RedditError):
            client.resolve("https://evil.example/comments/abc")
        self.assertEqual(fake.calls, [])

    def test_read_only_guard(self):
        client, fake, _ = self.client([])
        for method, url, source in [("POST", reddit.OAUTH + "/api/comment", "official"),
                                    ("GET", reddit.OAUTH + "/api/vote", "official"),
                                    ("POST", reddit.ARCTIC + "/posts/ids", "arctic"),
                                    ("DELETE", reddit.TOKEN_URL, "official")]:
            with self.assertRaisesRegex(reddit.RedditError, "read-only"):
                client.request(source, url, method=method)
        self.assertEqual(fake.calls, [])

    def test_reddit_cannot_be_a_redlib_instance(self):
        client, _, _ = self.client([], dict(self.env, REDLIB_INSTANCES=
            "https://reddit.com,https://old.reddit.com,https://redd.it,https://x:secret@example.org"))
        self.assertEqual(client.instances(), [])

    def test_secrets_not_in_errors_or_cli_output(self):
        secret_text = " ".join([ENV["REDDIT_CLIENT_SECRET"], ENV["REDDIT_REFRESH_TOKEN"],
                                 TOKEN["access_token"]])
        for reply in [RuntimeError(secret_text), (400, {}, secret_text.encode()),
                      response(dict(error=secret_text)), response(dict(TOKEN, scope="read vote"))]:
            client, _, _ = self.client([reply])
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                status = reddit.main(["thread", "abc", "--source", "official"], client)
            self.assertEqual(status, 1)
            self.assertIn("official API not available", error.getvalue())
            for secret in (ENV["REDDIT_CLIENT_SECRET"], ENV["REDDIT_REFRESH_TOKEN"], TOKEN["access_token"]):
                self.assertNotIn(secret, output.getvalue() + error.getvalue())

    def test_token_post_once_even_on_server_error(self):
        client, fake, _ = self.client([response({}, 503)])
        with self.assertRaisesRegex(reddit.RedditError, "official API not available"):
            client.official("abc", 200)
        self.assertEqual(len(fake.calls), 1)

    def test_credentials_file_env_precedence_and_no_default_state(self):
        reddit.save_credentials(self.env, {"client_id": "saved", "client_secret": "saved-secret",
                                            "refresh_token": "saved-refresh"})
        self.assertEqual(reddit.credentials(self.env)["client_id"], "fake-client")
        env = {"REDDIT_CONFIG_DIR": self.temporary.name, "REDDIT_STATE_DIR": self.env["REDDIT_STATE_DIR"]}
        self.assertEqual(reddit.credentials(env)["refresh_token"], "saved-refresh")
        self.assertFalse(Path(env["REDDIT_STATE_DIR"]).exists())
        self.assertEqual([p.name for p in Path(self.temporary.name).iterdir()], ["credentials"])

    def test_auth_read_only_consent_stores_token_without_echo(self):
        client, fake, _ = self.client([response(TOKEN)])
        output = []
        client.auth(output.append, lambda prompt: "FAKE_AUTH_CODE")
        text = "\n".join(output)
        consent = urllib.parse.parse_qs(urllib.parse.urlsplit(output[0].splitlines()[-1]).query)
        self.assertEqual(consent["scope"], ["read"])
        self.assertEqual(consent["duration"], ["permanent"])
        self.assertEqual(fake.calls[0][0:2], ("POST", reddit.TOKEN_URL))
        self.assertEqual(reddit.credentials({"REDDIT_CONFIG_DIR": self.temporary.name})["refresh_token"],
                         TOKEN["refresh_token"])
        for secret in (TOKEN["access_token"], TOKEN["refresh_token"], ENV["REDDIT_CLIENT_SECRET"], "FAKE_AUTH_CODE"):
            self.assertNotIn(secret, text)

    def test_auth_redirect_checks_state(self):
        client, fake, _ = self.client([])
        with self.assertRaisesRegex(reddit.RedditError, "state mismatch"):
            client.auth(lambda text: None, lambda prompt: "http://localhost:8080?code=secret&state=wrong")
        self.assertEqual(fake.calls, [])

    def test_sources_report_configured_health(self):
        env = dict(REDDIT_CONFIG_DIR=self.temporary.name, REDDIT_STATE_DIR=self.env["REDDIT_STATE_DIR"])
        client, _, _ = self.client([response(dict(data=[dict(POST, id="1xkn61s")])),
                                    response(dict(data=COMMENTS))], env)
        sources = client.sources()
        self.assertTrue(sources[0]["healthy"])
        self.assertFalse(sources[1]["configured"])
        self.assertFalse(sources[2]["configured"])

    def test_sources_probe_uses_env_or_module_default(self):
        for configured in (None, "custom123"):
            with self.subTest(configured=configured):
                env = dict(self.env)
                if configured:
                    env["REDDIT_PROBE_ID"] = configured
                client, _, _ = self.client([], env)
                with patch.object(reddit, "PROBE_THREAD_ID", "default123"), \
                        patch.object(client, "thread") as thread:
                    self.assertTrue(all(row["healthy"] for row in client.sources()))
                self.assertEqual(thread.call_args_list,
                                 [unittest.mock.call(configured or "default123", name, 1)
                                  for name in ("arctic", "redlib", "official")])

    def test_malformed_source_falls_back(self):
        client, _, _ = self.client([response(dict(data=[None])), (200, {}, HTML)])
        self.assertEqual(client.thread("abc")["source"], "redlib")

    def test_echoed_secret_in_success_payload_is_redacted(self):
        post = dict(POST, selftext=TOKEN["access_token"] + " " + ENV["REDDIT_CLIENT_SECRET"])
        client, _, _ = self.client([response(TOKEN), response(official_payload(post=post))])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reddit.main(["thread", "abc", "--source", "official", "--json"], client), 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["post"]["selftext"], "[REDACTED] [REDACTED]")

    def test_file_credentials_are_redacted_from_public_source(self):
        env = {"REDDIT_CONFIG_DIR": self.temporary.name}
        reddit.save_credentials(env, dict(client_id="saved", client_secret="FILE_SECRET",
                                           refresh_token="FILE_REFRESH"))
        client, _, _ = self.client([response(dict(data=[dict(POST, selftext="FILE_SECRET FILE_REFRESH")])),
                                    response(dict(data=COMMENTS))], env)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reddit.main(["thread", "abc"], client), 0)
        self.assertNotIn("FILE_SECRET", output.getvalue())
        self.assertNotIn("FILE_REFRESH", output.getvalue())

    def test_cycles_and_deep_comments_do_not_crash(self):
        comments = [dict(COMMENTS[0], id="a", parent_id="t1_b"),
                    dict(COMMENTS[1], id="b", parent_id="t1_a")]
        _, rows = reddit.normalize(POST, comments)
        self.assertEqual(len(reddit.ordered_comments(rows)), 2)
        deep = [dict(COMMENTS[0], id=str(n), parent_id="t1_" + str(n - 1) if n else "t3_abc")
                for n in range(1100)]
        _, rows = reddit.normalize(POST, deep)
        self.assertEqual(rows[-1]["depth"], 1099)
        self.assertEqual(len(reddit.ordered_comments(rows)), 1100)


if __name__ == "__main__":
    unittest.main()
