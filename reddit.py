#!/usr/bin/env python3
"""Read Reddit threads through Arctic Shift, Redlib, then read-only OAuth."""

from __future__ import annotations

import argparse
import base64
import configparser
from datetime import datetime, timedelta, timezone
import getpass
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH = "https://oauth.reddit.com"
USER_AGENT = "windows:karmiphuc-reddit-cli:v0.1 (by /u/karmiphuc)"
POST_FIELDS = ("id", "subreddit", "title", "author", "score", "num_comments",
               "created_utc", "url", "selftext")
COMMENT_FIELDS = ("id", "parent_id", "depth", "author", "score", "created_utc", "body")


class RedditError(Exception):
    """Only fixed, safe messages may cross this public error boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    # No redirects: a challenge or login redirect is a failed source, not a new request.
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def header(headers, name):
    return next((str(v) for k, v in headers.items() if k.lower() == name.lower()), "")


def is_challenge(status, headers, raw):
    body = raw.decode("utf-8", "replace")
    # A valid API payload may quote challenge pages: fetched prose remains data.
    if status == 200:
        try:
            if isinstance(json.loads(body), (dict, list)):
                body = ""
        except ValueError:
            pass
    text = (body + " " + header(headers, "Location")).lower()
    markers = ("js_challenge", "anubis", "captcha", "please wait for verification",
               "checking your browser", "cf-chl-", "challenge-platform")
    return any(marker in text for marker in markers) or (
        status in (403, 429) and "challenge" in text)


def config_dir(env):
    return Path(env.get("REDDIT_CONFIG_DIR") or Path.home() / ".config" / "reddit")


def credentials(env):
    parser = configparser.ConfigParser(interpolation=None)
    path = config_dir(env) / "credentials"
    try:
        if path.exists():
            parser.read_string(path.read_text(encoding="utf-8"))
        saved = parser["reddit"] if parser.has_section("reddit") else {}
        return {key: env.get("REDDIT_" + key.upper()) or saved.get(key, "")
                for key in ("client_id", "client_secret", "refresh_token", "redirect_uri")}
    except (OSError, configparser.Error, UnicodeError):
        raise RedditError("Cannot read Reddit credentials file.") from None


def save_credentials(env, values):
    directory = config_dir(env)
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser(interpolation=None)
        parser["reddit"] = values
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            parser.write(handle)
        temporary.replace(directory / "credentials")
        os.chmod(directory / "credentials", 0o600)
    except (OSError, ValueError, configparser.Error):
        raise RedditError("Cannot store Reddit credentials file.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def identifier(value):
    return str(value or "").removeprefix("t1_").removeprefix("t3_")


def number(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def normalize(post, comments):
    normalized = {key: post.get(key, "" if key not in
                  ("score", "num_comments", "created_utc") else 0) for key in POST_FIELDS}
    normalized["id"] = identifier(normalized["id"])
    normalized["subreddit"] = str(normalized["subreddit"]).removeprefix("r/")
    for key in ("score", "num_comments", "created_utc"):
        normalized[key] = number(normalized[key])
    rows = {}
    for comment in comments:
        cid = identifier(comment.get("id"))
        if not cid or cid in rows:
            continue
        parent = str(comment.get("parent_id") or "t3_" + normalized["id"])
        if not parent.startswith(("t1_", "t3_")):
            parent = ("t3_" if parent == normalized["id"] else "t1_") + parent
        rows[cid] = {"id": cid, "parent_id": parent, "depth": 0,
                     "author": comment.get("author") or "[deleted]",
                     "score": number(comment.get("score")),
                     "created_utc": number(comment.get("created_utc")),
                     "body": comment.get("body") or ""}
    # Iterative ancestry traversal avoids recursion failures on deep or malformed trees.
    for row in rows.values():
        parent = row["parent_id"]
        seen = {row["id"]}
        while parent.startswith("t1_") and parent[3:] in rows and parent[3:] not in seen:
            seen.add(parent[3:])
            row["depth"] += 1
            parent = rows[parent[3:]]["parent_id"]
    return normalized, list(rows.values())


def ordered_comments(comments):
    rows = {row["id"]: row for row in comments}
    children = {}
    for row in comments:
        parent = row["parent_id"]
        key = parent[3:] if parent.startswith("t1_") and parent[3:] in rows else None
        children.setdefault(key, []).append(row)
    for group in children.values():
        group.sort(key=lambda row: (-row["score"], row["id"]))
    stack = [(row, 0) for row in reversed(children.get(None, []))]
    seen = set()
    result = []
    while stack:
        row, depth = stack.pop()
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        result.append((row, depth))
        stack.extend((child, depth + 1) for child in reversed(children.get(row["id"], [])))
    result.extend((row, 0) for row in comments if row["id"] not in seen)
    return result


def markdown(result):
    post = result["post"]
    lines = ["# " + str(post["title"]), "",
             f"r/{post['subreddit']} | u/{post['author']} | score {post['score']}",
             str(post["url"]), "", str(post["selftext"] or ""), "",
             f"Comments: {result['coverage']['returned']}/{result['coverage']['reported_total']}"
             f" (source: {result['source']})", ""]
    for row, depth in ordered_comments(result["comments"]):
        indent = "  " * depth
        lines.append(f"{indent}- u/{row['author']} | score {row['score']} | {row['id']}")
        lines.extend(indent + "  " + line for line in str(row["body"]).splitlines())
    return "\n".join(lines)


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def has(self, name):
        return name in self.attrs.get("class", "").split()

    def find(self, name):
        return next((node for node in self.walk() if node.has(name)), None)

    def walk(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in reversed(node.children) if isinstance(child, Node))

    def text(self):
        output = []
        stack = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                output.append(node)
            else:
                if node.tag in ("br", "p", "div", "li"):
                    output.append("\n")
                stack.extend(reversed(node.children))
        return "".join(output).strip()


class RedlibParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"):
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def node_text(node, name, default=""):
    found = node.find(name)
    return found.text() if found is not None else default


def html_number(node, name):
    found = node.find(name)
    if found is None:
        return 0
    text = found.attrs.get("title", found.text()).replace(",", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([km]?)", text.lower())
    return int(float(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]) if match else 0


def html_time(node):
    found = next((item for item in node.walk() if item.tag == "time"), None)
    if found is None:
        return 0
    value = found.attrs.get("datetime") or found.attrs.get("title", "")
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def redlib_thread(raw, post_id):
    parser = RedlibParser()
    parser.feed(raw.decode("utf-8", "replace"))
    post_node = parser.root.find("post")
    if post_node is None or not node_text(post_node, "post_title"):
        raise RedditError("Redlib returned no recognizable post.")
    title_node = post_node.find("post_title")
    link = next((node.attrs.get("href", "") for node in title_node.walk()
                 if node.tag == "a"), "")
    post = dict(id=post_id, title=title_node.text(),
                subreddit=node_text(post_node, "post_subreddit").removeprefix("r/"),
                author=node_text(post_node, "post_author").removeprefix("u/"),
                score=html_number(post_node, "post_score"),
                num_comments=html_number(post_node, "post_comments"),
                created_utc=html_time(post_node), url=urllib.parse.urljoin("https://www.reddit.com", link),
                selftext=node_text(post_node, "post_body"))
    # Some Redlib versions use comment_count rather than post_comments.
    if post_node.find("post_comments") is None:
        post["num_comments"] = html_number(post_node, "comment_count")
    comments = []
    for node in parser.root.walk():
        if not node.has("comment"):
            continue
        cid = identifier(node.attrs.get("id"))
        if not cid:
            continue
        parent = node.parent
        while parent is not None and not parent.has("comment"):
            parent = parent.parent
        parent_id = "t1_" + identifier(parent.attrs.get("id")) if parent else "t3_" + post_id
        comments.append(dict(id=cid, parent_id=parent_id,
                             author=node_text(node, "comment_author").removeprefix("u/"),
                             score=html_number(node, "comment_score"),
                             created_utc=html_time(node), body=node_text(node, "comment_body")))
    if not comments and post_node.find("post_comments") is None and post_node.find("comment_count") is None:
        raise RedditError("Redlib returned no comment count or comments.")
    return normalize(post, comments)


class Client:
    def __init__(self, env=None, transport=urllib_transport, sleep=time.sleep, clock=time.monotonic):
        self.env = os.environ if env is None else env
        self.transport, self.sleep, self.clock = transport, sleep, clock
        self.last_request = {}
        self._secrets = set()
        self.remember_secrets(self.env.get("REDDIT_CLIENT_SECRET"), self.env.get("REDDIT_REFRESH_TOKEN"))
        try:
            saved = credentials(self.env)
            self.remember_secrets(saved["client_secret"], saved["refresh_token"])
        except RedditError:
            # Unusable official credentials must not prevent public archive access.
            pass

    def remember_secrets(self, *values):
        self._secrets.update(value for value in values if isinstance(value, str) and value)

    def redact(self, text):
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(json.dumps(value, ensure_ascii=True)[1:-1], "[REDACTED]")
            text = text.replace(value, "[REDACTED]")
        return text

    def request(self, source, url, method="GET", data=None, extra_headers=None):
        parsed = urllib.parse.urlsplit(url)
        allowed = (source == "arctic" and url.startswith(ARCTIC + "/")) or (
            source == "official" and (url == TOKEN_URL or
                                      url.startswith(OAUTH + "/comments/"))) or (
            source == "redlib" and any(url.startswith(base + "/") for base in self.instances()))
        if not allowed or parsed.username or parsed.password or (
                method != "GET" and not (method == "POST" and url == TOKEN_URL)):
            raise RedditError("Request refused by read-only policy.")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        headers.update(extra_headers or {})
        # Redlib is tried once per instance, even on transient errors.
        tries = 1 if source == "redlib" or url == TOKEN_URL else 3
        for attempt in range(tries):
            if source in self.last_request:
                self.sleep(max(0, 1 - (self.clock() - self.last_request[source])))
            self.last_request[source] = self.clock()
            try:
                status, response_headers, raw = self.transport(method, url, headers, data)
            except Exception:
                raise RedditError("Source transport failed.") from None
            if is_challenge(status, response_headers, raw):
                raise RedditError("Challenge detected; source skipped.")
            if status == 429 or 500 <= status < 600:
                wait = header(response_headers, "Retry-After")
                delay = min(60, max(1, int(wait))) if wait.isdigit() else 2 ** (attempt + 1)
                self.sleep(delay)
                if attempt + 1 < tries:
                    continue
            if status != 200:
                raise RedditError(f"Source returned HTTP {status}.")
            return raw
        raise RedditError("Source unavailable.")

    def json_request(self, source, url, **kwargs):
        raw = self.request(source, url, **kwargs)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise RedditError("Source returned invalid JSON.") from None

    def arctic_data(self, path, params):
        payload = self.json_request("arctic", ARCTIC + path + "?" + urllib.parse.urlencode(params))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RedditError("Archive returned invalid data.")
        if not all(isinstance(row, dict) for row in payload["data"]):
            raise RedditError("Archive returned invalid data.")
        return payload["data"]

    def pages(self, path, params):
        seen = set()
        before = params.get("before")
        while True:
            query = dict(params, limit=100, sort="desc")
            if before is not None:
                query["before"] = before
            batch = self.arctic_data(path, query)
            fresh = [row for row in batch if row.get("id") not in seen]
            for row in fresh:
                seen.add(row.get("id"))
                yield row
            if len(batch) < 100:
                return
            if not fresh:
                raise RedditError("Archive pagination stalled; results may be incomplete.")
            oldest = min(number(row.get("created_utc")) for row in batch)
            if not oldest:
                raise RedditError("Archive pagination has no timestamp.")
            # Overlap the boundary second so comments sharing a timestamp are not dropped.
            cursor = oldest + 1
            if before is not None and cursor >= float(before):
                raise RedditError("Archive pagination stalled at a timestamp boundary.")
            before = cursor

    def arctic(self, post_id, limit):
        posts = self.arctic_data("/posts/ids", {"ids": post_id})
        if not posts:
            raise RedditError("Archive has no post.")
        comments = []
        for row in self.pages("/comments/search", {"link_id": post_id}):
            comments.append(row)
            if len(comments) >= max(1, limit):
                break
        return normalize(posts[0], comments)

    def instances(self):
        result = []
        for value in self.env.get("REDLIB_INSTANCES", "").split(","):
            value = value.strip().rstrip("/")
            parsed = urllib.parse.urlsplit(value)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme in ("https", "http") and hostname and not parsed.username
                    and not parsed.password and not parsed.query and not parsed.fragment
                    and hostname != "redd.it" and not hostname.endswith(".redd.it")
                    and hostname != "reddit.com" and not hostname.endswith(".reddit.com")):
                if value not in result:
                    result.append(value)
        return result

    def token(self, values, grant):
        self.remember_secrets(values["client_secret"], values["refresh_token"], grant.get("code"))
        basic = base64.b64encode((values["client_id"] + ":" + values["client_secret"]).encode()).decode()
        try:
            payload = self.json_request("official", TOKEN_URL, method="POST",
                                        data=urllib.parse.urlencode(grant).encode(),
                                        extra_headers={"Authorization": "Basic " + basic,
                                                       "Content-Type": "application/x-www-form-urlencoded"})
            if not isinstance(payload, dict) or payload.get("error"):
                raise RedditError("Invalid token response.")
            scope = set(str(payload.get("scope", "")).split())
            if scope != {"read"}:
                raise RedditError("Token must have read scope only.")
            if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
                raise RedditError("Missing token.")
            self.remember_secrets(payload["access_token"], payload.get("refresh_token"))
            return payload
        except RedditError:
            raise RedditError("official API not available (token request failed; read scope required).") from None

    def official(self, post_id, limit):
        values = credentials(self.env)
        if not values["client_id"] or not values["refresh_token"]:
            raise RedditError("official API not available (configure credentials or run reddit auth).")
        payload = self.token(values, {"grant_type": "refresh_token", "refresh_token": values["refresh_token"]})
        try:
            listings = self.json_request("official", OAUTH + "/comments/" + post_id + "?" +
                                         urllib.parse.urlencode({"limit": min(500, max(1, limit)),
                                                                 "sort": "top", "raw_json": 1}),
                                         extra_headers={"Authorization": "Bearer " + payload["access_token"]})
            post = listings[0]["data"]["children"][0]["data"]
            stack = [(item, "t3_" + post_id) for item in reversed(listings[1]["data"]["children"])]
            comments = []
            while stack:
                item, parent_id = stack.pop()
                if item.get("kind") != "t1":
                    continue
                row = dict(item["data"])
                row["parent_id"] = row.get("parent_id") or parent_id
                comments.append(row)
                replies = row.get("replies")
                if isinstance(replies, dict):
                    stack.extend((child, "t1_" + identifier(row.get("id")))
                                 for child in reversed(replies["data"]["children"]))
            return normalize(post, comments)
        except (RedditError, KeyError, IndexError, TypeError, AttributeError):
            raise RedditError("official API not available (thread request failed).") from None

    def resolve(self, target, title=None, saved_at=None):
        if re.fullmatch(r"[a-z0-9]+", target):
            return target
        parsed = urllib.parse.urlsplit(target)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http") or host not in (
                "reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "m.reddit.com", "redd.it"):
            raise RedditError("Expected a Reddit post URL, share URL, or bare post id.")
        match = re.fullmatch(r"/r/([A-Za-z0-9_]+)/s/([A-Za-z0-9]+)/?", parsed.path)
        if match:
            if not title or not saved_at:
                raise RedditError("Share links require --title and --saved-at.")
            try:
                saved = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                if saved.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise RedditError("--saved-at must be ISO8601 with a timezone.") from None
            params = {"subreddit": match[1], "after": int((saved - timedelta(days=3)).timestamp()),
                      "before": int((saved + timedelta(hours=1)).timestamp())}
            wanted = " ".join(title.casefold().split())
            matches = set()
            if not wanted:
                raise RedditError("Share links require a nonempty title.")
            for post in self.pages("/posts/search", params):
                candidate = " ".join(str(post.get("title", "")).casefold().split())
                if candidate and (candidate == wanted or candidate.startswith(wanted)):
                    matches.add(identifier(post.get("id")))
            if len(matches) != 1:
                raise RedditError("Share-link title has no unique match in the save-time window.")
            return matches.pop()
        match = re.match(r"/(?:r/[^/]+/)?comments/([a-z0-9]+)(?:/|$)", parsed.path)
        if match:
            return match[1]
        if host == "redd.it" and re.fullmatch(r"/[a-z0-9]+/?", parsed.path):
            return parsed.path.strip("/")
        raise RedditError("Unrecognized Reddit URL.")

    def thread(self, post_id, source="auto", limit=200):
        attempts = []
        for name in (("arctic", "redlib", "official") if source == "auto" else (source,)):
            bases = self.instances() if name == "redlib" else [None]
            if not bases:
                attempts.append(dict(source=name, ok=False, note="No Redlib instances configured."))
            for base in bases:
                try:
                    if name == "arctic":
                        post, comments = self.arctic(post_id, limit)
                    elif name == "official":
                        post, comments = self.official(post_id, limit)
                    else:
                        post, comments = redlib_thread(self.request(name, base + "/comments/" + post_id), post_id)
                    if post["id"] != post_id or not post["title"] or (not comments and post["num_comments"] != 0):
                        raise RedditError("Source returned no complete thread.")
                    attempts.append(dict(source=name, ok=True, note=""))
                    comments = [row for row, _ in ordered_comments(comments)][:limit]
                    return dict(source=name, post=post, comments=comments,
                                coverage=dict(returned=len(comments), reported_total=post["num_comments"]),
                                attempts=attempts)
                except RedditError as exc:
                    attempts.append(dict(source=name, ok=False, note=str(exc)))
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    attempts.append(dict(source=name, ok=False, note="Source returned invalid thread data."))
        detail = "; ".join(attempt["source"] + ": " + attempt["note"] for attempt in attempts)
        raise RedditError("No source returned the post and comments. " + detail)

    def sources(self):
        result = []
        for name in ("arctic", "redlib", "official"):
            configured = name == "arctic" or (bool(self.instances()) if name == "redlib" else
                         bool(credentials(self.env)["client_id"] and credentials(self.env)["refresh_token"]))
            row = dict(source=name, configured=configured, healthy=False, note="Not configured.")
            if configured:
                try:
                    self.thread("1xkn61s", name, 1)
                    row.update(healthy=True, note="Thread probe succeeded.")
                except RedditError as exc:
                    row["note"] = str(exc)
            result.append(row)
        return result

    def auth(self, output=print, read_code=getpass.getpass):
        values = credentials(self.env)
        if not values["client_id"]:
            raise RedditError("Set REDDIT_CLIENT_ID before running reddit auth.")
        redirect = values["redirect_uri"] or "http://localhost:8080"
        state = secrets.token_urlsafe(24)
        url = "https://www.reddit.com/api/v1/authorize?" + urllib.parse.urlencode({
            "client_id": values["client_id"], "response_type": "code", "state": state,
            "redirect_uri": redirect, "duration": "permanent", "scope": "read"})
        output("Open this URL in your browser and approve read-only access:\n" + url)
        code = read_code("Paste the authorization code (hidden): ").strip()
        if code.startswith(("https://", "http://")):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(code).query)
            if query.get("state") != [state] or query.get("error"):
                raise RedditError("Authorization state mismatch or consent denied.")
            code = query.get("code", [""])[0]
        if not code:
            raise RedditError("No authorization code supplied.")
        payload = self.token(values, {"grant_type": "authorization_code", "code": code,
                                      "redirect_uri": redirect})
        if not isinstance(payload.get("refresh_token"), str) or not payload["refresh_token"]:
            raise RedditError("official API not available (no refresh token returned).")
        values.update(refresh_token=payload["refresh_token"], redirect_uri=redirect)
        save_credentials(self.env, values)
        output("Read-only credentials stored. Refresh token was not displayed.")


def nonnegative(value):
    try:
        result = int(value)
        if result >= 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a nonnegative integer")


def utf8_output(stream):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None, client=None):
    utf8_output(sys.stdout)
    utf8_output(sys.stderr)
    parser = argparse.ArgumentParser(prog="reddit", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    thread = commands.add_parser("thread", help="Fetch a post and its comments")
    thread.add_argument("target")
    thread.add_argument("--title")
    thread.add_argument("--saved-at")
    thread.add_argument("--max-comments", type=nonnegative, default=200)
    thread.add_argument("--source", choices=("auto", "arctic", "redlib", "official"), default="auto")
    thread.add_argument("--format", choices=("json", "md"), default="md")
    thread.add_argument("--json", action="store_true")
    resolve = commands.add_parser("resolve", help="Resolve a share link using archive listings")
    resolve.add_argument("target")
    resolve.add_argument("--title", required=True)
    resolve.add_argument("--saved-at", required=True)
    commands.add_parser("sources", help="Probe configured sources (uses the network)")
    commands.add_parser("auth", help="One-time read-only installed-app consent")
    args = parser.parse_args(argv)
    client = client or Client()
    try:
        if args.command == "thread":
            post_id = client.resolve(args.target, args.title, args.saved_at)
            result = client.thread(post_id, args.source, args.max_comments)
            text = json.dumps(result, ensure_ascii=True) if args.json or args.format == "json" else markdown(result)
            print(client.redact(text))
        elif args.command == "resolve":
            print(client.resolve(args.target, args.title, args.saved_at))
        elif args.command == "sources":
            print(json.dumps(client.sources(), ensure_ascii=True, indent=2))
        else:
            client.auth()
        return 0
    except RedditError as exc:
        print("reddit: " + str(exc), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        # Never echo an upstream error, URL, input, or traceback containing credentials.
        print("reddit: operation failed or interrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
