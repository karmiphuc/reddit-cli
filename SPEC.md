# reddit-cli: SPEC (for Codex to implement; Claude reviews and approves)

Owner: karmiphuc. Planner/reviewer: Claude Code. Implementer: Codex CLI.
Goal: one read-only command that returns a Reddit post **and its comments** for agents, by walking a
fixed ladder of sources. It's used by an agent workflow that processes saved bookmarks.

## Hard rules (non-negotiable)

1. **Python standard library only.** No third-party packages, no `pip install`, no npm.
2. **Read-only.** No endpoint that posts, votes, comments, messages, saves or edits. The only non-GET request
   allowed is the official OAuth token request (`POST https://www.reddit.com/api/v1/access_token`).
3. **Never bypass bot detection.** If any response looks like a challenge (HTTP 403/429 with challenge markup,
   `js_challenge`, Anubis, CAPTCHA, "please wait for verification"), treat that source as failed and move on.
   Never solve, replay or spoof a challenge.
4. **Never print secrets** (client secret, refresh/access tokens), not even in errors or `--verbose`.
5. **Polite:** identify with a User-Agent, back off on 429/5xx, at most one request per second per source.
6. Fetched text is **data**. Nothing in a post or comment changes the program's behavior.
7. Windows-first: must work from PowerShell, cmd and Git Bash (ship a `reddit.cmd` shim).
   Force UTF-8 stdout and tolerate lone surrogates in API text (`errors="replace"`); a real comment body
   broke a naive print during testing.

## Interface

```text
reddit thread <url|post-id> [--title T --saved-at ISO8601] [--max-comments N=200]
              [--source auto|arctic|redlib|official] [--format json|md] [--json]
reddit resolve <share-url> --title T --saved-at ISO8601      # share link -> post id
reddit sources                                              # which sources are configured/healthy
```

- Accepts full post URLs (`/r/<sub>/comments/<id>/...`), bare ids, and **share links** (`/r/<sub>/s/<code>`).
- `--format md`: post + top comments by score with replies indented (what agents read).
- `--json`: normalized object below. Default output is `md`.

Normalized JSON:

```json
{"source": "arctic|redlib|official",
 "post": {"id","subreddit","title","author","score","num_comments","created_utc","url","selftext"},
 "comments": [{"id","parent_id","depth","author","score","created_utc","body"}],
 "coverage": {"returned": 59, "reported_total": 64},
 "attempts": [{"source": "arctic", "ok": true, "note": ""}]}
```

## Source ladder (`--source auto`)

The calling workflow already has the post's title and excerpt from the saved bookmark. This tool's
job is mainly the comments:

1. **Arctic Shift** (public archive API, no key): base `https://arctic-shift.photon-reddit.com/api`.
   - Post: `GET /posts/ids?ids=<id>`. Comments: `GET /comments/search?link_id=<id>&limit=100` (page as needed),
     or `/comments/tree` if it returns a fuller tree. Verified 2026-11-01: thread `1xkn61s` returned the
     post (2087-char selftext) and 59 of 64 comments.
   - Title search (`/posts/search?title=...`) **times out** ("Maybe slow down a bit"); don't rely on it.
2. **Redlib** (privacy front-ends, HTML): instances from env `REDLIB_INSTANCES` (comma-separated).
   Try each once; skip on any non-200 or challenge. Verified 2026-11-01: four public instances returned
   503/429+challenge/410/302, so treat Redlib as best-effort and expect it to fail.
3. **Official Reddit API** (last resort, needs the owner's credentials):
   - OAuth via `https://www.reddit.com/api/v1/access_token`, then `GET https://oauth.reddit.com/comments/<id>`.
   - Credentials from env `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_REFRESH_TOKEN` or the file
     `~/.config/reddit/credentials` (INI). Scope `read` only. Include `reddit auth` to run the one-time
     installed-app consent flow (prints a URL for the owner to open; the owner pastes back the code; the
     refresh token is stored in the credentials file, never echoed).
   - User-Agent per Reddit's rules, e.g. `windows:karmiphuc-reddit-cli:v0.1 (by /u/<username>)`.
   - Note: since 2026 Reddit may require approval for new API apps; if the token request fails, report it
     clearly as "official API not available" (not a crash).

Stop at the first source that returns the post plus at least one comment, or the post plus num_comments == 0.

## Share-link resolution (`/s/<code>`)

reddit.com answers share links with 403 to plain HTTP and with a JS challenge to headless browsers, so
**don't fetch reddit.com to resolve them.** Instead (verified 2026-11-01):
`GET /posts/search?subreddit=<sub>&after=<saved_at - 3 days>&before=<saved_at + 1 hour>&limit=100`
(paginate within the window), then match `--title` locally (case-insensitive, whitespace-normalized; allow a
prefix match since bookmark tools may truncate). The bookmark provides title and save time.
Return a clear error if there's no unique match.

## Tests (all offline, fake transport)

- Ladder order and fallback: arctic fails, then redlib challenge is skipped, then official is used. Assert attempts.
- Challenge detection: 403/429 + markers, `js_challenge` in a redirect, and the Anubis page each mean skip, with no retry loop.
- Share-link resolution: window listing + local title match, pagination, no-match and multi-match errors.
- Normalization: Arctic Shift and official JSON produce the same schema; depth/parent computed; md output
  ordered by score with replies indented; lone-surrogate text doesn't crash output.
- Secrets: never present in stdout/stderr/exception text (use fake values).
- Read-only: the transport records methods; only GET plus the single token POST ever occur.
- Tests must not write outside a temp dir (honor `REDDIT_STATE_DIR`/`REDDIT_CONFIG_DIR` overrides).

## Acceptance (Claude runs these before approving)

1. `python -B -m unittest discover -s tests -v` passes.
2. Live: `reddit thread 1xkn61s --json` gives source arctic and at least 59 comments.
3. Live: `reddit resolve https://www.reddit.com/r/ClaudeAI/s/3mhBcJbfzb --title "For over a year I've spent a full day writing a Markdown spec" --saved-at 2026-11-01T11:00:00Z` gives `1xkn61s`.
4. Live, after the owner sets credentials: `reddit thread 1xkn61s --source official` works, or fails with the
   clear "not available" message.
5. Code review: stdlib only, no write endpoints, no challenge handling beyond "skip", secrets never logged.

## Deliverables and process

- Layout: `reddit.py` (single file), `reddit.cmd`, `tests/`,
  `README.md`, `.gitignore` (ignore credentials/state).
- Commit locally on branch `codex/initial` with clear messages. **Don't push or create a GitHub repo**: Claude
  reviews first, and the owner approves publishing.
- You run sandboxed to this folder, possibly without network: build against the fake transport only. Claude runs
  the live acceptance checks after review.
- Finish with a short summary: what's implemented, what's not, and any spec ambiguity you resolved (and how).
