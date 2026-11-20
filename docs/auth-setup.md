# Authentication Setup Guide

This server supports two authentication modes:

1. direct token (`META_ACCESS_TOKEN`)
2. local OAuth (`META_APP_ID` + `META_APP_SECRET`)

## Option 1: Direct Access Token

Set a valid Meta access token in the server environment.

```bash
export META_ACCESS_TOKEN="EA..."
armavita-meta-ads-mcp
```

Notes:

- token scope must include ad account access for the tools you call
- token expiry and revoked permissions will cause auth errors at runtime

## Option 2: Local OAuth Login

Prerequisites:

1. A Meta app with valid Ads permissions, **in Development mode**. A Live (published) app
   accepts only HTTPS redirect URIs, so it refuses `http://localhost` and `--login` can't
   use it, even when the app is approved and the URI is on every list (Malorie, Nov 17).
   Keep a separate development app for local logins. You, as its admin or developer, can
   sign in to it without review.
2. `META_APP_ID` and `META_APP_SECRET` for that app.
3. The app set up for a localhost callback, in the Meta developer dashboard:
   - **App settings → Basic → App Domains:** `localhost`. Without it, Facebook shows
     "Can't load URL: The domain of this URL isn't included in the app's domains".
   - **App settings → Basic → Add platform → Website**, with Site URL `http://localhost:8080/`.
   - **Facebook Login → Settings → Valid OAuth Redirect URIs:** `http://localhost:8080/callback`.
     If 8080 is busy, `--login` uses 8081–8089 instead. The terminal prints the URL it
     opens, so check `redirect_uri` there and add that port too.
   - **App settings → Advanced:** "Native or desktop app" **on**, and "Is app secret
     embedded in the client" **off**. With the second on, Facebook refuses the code exchange
     with Graph error 1 (Oct 31).

Run, on Linux or macOS:

```bash
export META_APP_ID="..."
read -rs META_APP_SECRET; export META_APP_SECRET   # silent prompt: paste, then Enter
armavita-meta-ads-mcp --login
```

On Windows (PowerShell):

```powershell
$env:META_APP_ID = "..."
$s = Read-Host "App secret" -AsSecureString
$env:META_APP_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
armavita-meta-ads-mcp --login
```

The login command opens a browser, and after Facebook signs you in it lands on a
"Connected to Meta" page and stores the token cache locally. With no app ID or secret
set, it stops with a message instead of opening the browser.

### Check which copy you're running

An old install can sit ahead of a new one on your PATH. That's common on Windows after
moving from `pip install --user` to `uv tool install`, and the old copy then behaves as
if fixes never shipped. Check:

```powershell
Get-Command armavita-meta-ads-mcp -All | Select-Object Source   # Windows
armavita-meta-ads-mcp --version                                  # prints the git commit too
```

```bash
which -a armavita-meta-ads-mcp                                   # Linux / macOS
```

If more than one turns up, remove the pip copy (`py -3.12 -m pip uninstall -y
armavita-meta-ads-mcp`, or `pip uninstall` on Linux) and keep the uv one.

## Token Cache Location

Cached token file is stored per OS:

- Linux: `~/.config/armavita-meta-ads-mcp/token_cache.json`
- macOS: `~/Library/Application Support/armavita-meta-ads-mcp/token_cache.json`
- Windows: `%APPDATA%\\armavita-meta-ads-mcp\\token_cache.json`

## Common Errors

`Authentication Required`:

- set `META_ACCESS_TOKEN`, or
- run `--login` with valid `META_APP_ID` and `META_APP_SECRET`

`No valid META_APP_ID configured`:

- set `META_APP_ID` in the MCP server environment used by your client launcher

`Authorization code exchange failed`:

- verify app secret, redirect URI, and permission approval status
- "Is app secret embedded in the client" must be off (App settings → Advanced)

`Can't load URL` (Facebook's page, before any login):

- add `localhost` to App Domains and a Website platform with Site URL `http://localhost:8080/`
- a Live app can't use a localhost redirect at all: use a Development-mode app

`Invalid App ID` (Facebook's page):

- you're running a copy from before ADS-83, or META_APP_ID names an app that doesn't exist; see "Check which copy you're running" above

`Invalid OAuth access token` / Meta error `code: 190`:

- token expired, revoked, or app secret changed
- if using direct token, generate a fresh token and restart the MCP server
- if using OAuth cache, rerun `armavita-meta-ads-mcp --login` to refresh the cached token

`Insufficient permission` / Meta error `code: 200` or `code: 10`:

- the token lacks required Meta Ads permissions/scopes
- confirm the app and user have approved the needed Ads scopes in Meta App Dashboard
- re-authenticate after updating scopes to issue a token with the new grants

`No ad account access` or empty account list:

- the authenticated user/token can be valid but not have access to the target ad account
- verify ad account assignment in Meta Business Manager and user role permissions
- check that you are querying the correct account ID format (`act_<ID>`)
