# reddit-cli

A Python standard-library-only, read-only CLI that fetches a Reddit post **and
its comments** for agents. Python 3.10+; no packages or installation required.

## Run

Put this folder on PATH to use `reddit.cmd` from PowerShell, cmd, or Git Bash.
The shim finds `reddit.py` relative to itself and requires `python` on PATH.
Alternatively run `python -B reddit.py ...` from this folder.

```text
reddit thread 1xkn61s
reddit thread https://www.reddit.com/r/ClaudeAI/comments/1xkn61s/ --json
reddit thread 1xkn61s --max-comments 100 --source arctic --format md
reddit resolve https://www.reddit.com/r/ClaudeAI/s/3mhBcJbfzb --title "For over a year I've spent a full day writing a Markdown spec" --saved-at 2026-11-01T11:00:00Z
reddit sources
reddit auth
```

`thread` accepts bare post IDs, post URLs, short `redd.it` URLs, and share links.
Share links require both `--title` and `--saved-at` on `thread`, too. The timestamp
must include a timezone. The default is Markdown; `--json` or `--format json`
returns the normalized object defined in SPEC.md. Markdown orders sibling
comments by descending score and indents replies underneath their parent.
Both stdout and stderr use UTF-8 with replacement for lone surrogates.

## Sources and coverage

The default ladder is Arctic Shift, configured Redlib instances, then official
Reddit OAuth. It stops at the first post with at least one comment, or a post
reporting zero comments. `--source` selects only that source. Failed attempts are
included in successful JSON output; total failure exits with code 1 and a safe
error on stderr. `sources` actively probes a sample thread and reports source
configuration and health as JSON; a failed probe is reported in the result.

Arctic Shift uses `/posts/ids` and paginated `/comments/search`. Pages overlap
their timestamp boundary and are deduplicated. A stalled boundary (for example,
100 entries with the same second) fails clearly rather than silently skipping
entries. The display/fetch cap defaults to 200. Arctic retrieves recent comments
until that cap, then orders the retrieved tree by score. Redlib and official
results are likewise capped after ordering; the official endpoint requests up
to 500 comments and preserves the returned reply tree. Reddit `more` placeholders
are not expanded. Coverage always reports the actual returned count against the
post's reported total, which may exceed archive/front-end availability.
`--max-comments 0` suppresses displayed comments while still checking that a
source meets the post-plus-comments success rule.

Share links are **never fetched from reddit.com**. Resolution lists posts in the
subreddit between save time minus three days and save time plus one hour, pages
within that window, and compares titles locally with case and whitespace
normalization. A saved title may be a prefix of the actual title. Exactly one
matching post is required, even if one match is exact and another is a prefix.
No slow archive title-search query is used.

Set `REDLIB_INSTANCES` to comma-separated instance roots, for example in PowerShell:

```powershell
$env:REDLIB_INSTANCES = 'https://your-redlib-instance.example'
```

Redlib is best-effort HTML parsing. Instances are tried once apiece; any non-200,
unrecognized page, or challenge fails that attempt. Reddit domains are rejected
as instance roots. No public instances are hardcoded.

## Official read-only OAuth

Configure an installed-app client with Reddit and register a redirect URI
(default: `http://localhost:8080`). Reddit may require approval for API apps.
The CLI prints **official API not available** if token acquisition fails.

Credentials come from `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and
`REDDIT_REFRESH_TOKEN`, with per-key fallback to `~/.config/reddit/credentials`.
Installed apps may use an empty client secret. Optional `REDDIT_REDIRECT_URI`
overrides the registered callback. The INI format is:

```ini
[reddit]
client_id = YOUR_APP_ID
client_secret =
refresh_token = YOUR_REFRESH_TOKEN
redirect_uri = http://localhost:8080
```

To obtain a refresh token, set the client ID and run `reddit auth`. Open the
printed consent URL yourself, approve only `read`, then paste the authorization
code into the hidden prompt. You can also paste the full callback URL, which
allows the CLI to validate its state parameter. No callback server is started;
copy the code/URL from the browser address bar even if localhost does not load.
The token is stored atomically in the credentials file and never echoed. Token
responses granting anything beyond exactly `read` are rejected. Credentials are
private: on POSIX the file has mode 0600; on Windows protect the directory with
your account's normal ACLs. The program does not modify Windows ACLs.

`REDDIT_CONFIG_DIR` overrides the credentials directory. The CLI keeps no token
cache or other persistent state, so `REDDIT_STATE_DIR` requires no files and never
causes a write elsewhere. `auth` is the only command that writes a local file.

## Safety and verification

The only HTTP methods are GET and POST to the fixed official token URL. No
write API endpoints are called. Requests identify with
`windows:karmiphuc-reddit-cli:v0.1 (by /u/karmiphuc)`, have a 30-second timeout,
and run at most once per second per source within a process. Non-challenge
429/5xx responses back off, honoring numeric Retry-After up to 60 seconds.
Archive and official thread GETs have at most three attempts. Token POSTs and
Redlib instances are attempted once. Redirects are never followed. Challenge
pages are skipped without solving, replaying, or spoofing anything. Fetched
post/comment text remains data; challenge words inside valid JSON prose do not
trigger challenge handling. Raw upstream errors and tracebacks are never
printed; known credentials and acquired tokens are redacted from thread output.

All tests run offline with a fake transport, fake credentials, and temporary
configuration/state directories:

```text
python -B -m unittest discover -s tests -v
```

Live acceptance checks from SPEC.md are intentionally left for Claude and the
owner. Implementation is local on `codex/initial`; publishing requires their
review. No memory/board sync, push, or GitHub repository creation is performed.
