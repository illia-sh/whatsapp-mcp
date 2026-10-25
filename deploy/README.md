# Hosting on a server

This guide runs the bridge and the MCP server around the clock on a Linux host, so Claude can reach your WhatsApp without your own computer being on. Claude connects to it as a **custom connector** over HTTPS.

It was written against Oracle Linux 9 on a 1 GB VM, and the commands use `dnf`/`firewalld` where it matters. Other systemd-based distributions work the same way with their own package manager and firewall.

> **Read this first.** The endpoint you are about to publish can read and send every message on the linked WhatsApp account. It is protected by a bearer token, and that token is the only thing standing between the internet and your messages. Treat it like a password, and see [Security notes](#security-notes) before you start.

## How it fits together

```
Claude (Anthropic's servers)
   │  HTTPS, Authorization: Bearer <token>
   ▼
Caddy :443 ── TLS certificate from Let's Encrypt
   ├─ /personal/*  →  MCP server 127.0.0.1:9000  →  bridge 127.0.0.1:8080  →  WhatsApp
   └─ /work/*      →  MCP server 127.0.0.1:9001  →  bridge 127.0.0.1:8081  →  WhatsApp
```

- Claude reaches custom connectors **from Anthropic's infrastructure**, not from your computer, so the endpoint has to be a public HTTPS address. A server reachable only over a VPN or private network won't work.
- Each WhatsApp account gets its own bridge, MCP server, port pair, store directory and token. One account is fine; the examples use two, named `personal` and `work`.
- Everything except Caddy listens on `127.0.0.1` only.

## What you need

- A Linux host with systemd and a public IP. x86_64 or arm64; 1 GB of RAM is enough for two accounts (see [Small hosts](#small-hosts)).
- A domain name whose A record points at the host. A free [DuckDNS](https://www.duckdns.org) subdomain works.
- Inbound **TCP 443** open to the internet, plus SSH for yourself. Nothing else.
- Docker on your own computer, to build the bridge. (You can build on the host instead if it has 2 GB of RAM or more.)
- Claude custom connectors with **Request headers**, shown in the *Add custom connector* dialog. Anthropic describes request-header authentication as a beta that isn't available to every organization yet; without it, Claude can't send the token, so check before you start.

The commands below run on **your computer** unless marked **on the host**. Replace `<host>` with your SSH destination and `<your-domain>` with your hostname.

## 1. Build the bridge

The bridge uses SQLite through cgo, so it needs a C toolchain for the target platform. This builds a fully static binary in Docker, which then runs on any Linux regardless of its C library version. `git archive` sends only committed files, so your local message stores never enter the container.

```bash
mkdir -p dist
git archive --format=tar HEAD whatsapp-bridge | docker run --rm -i --platform linux/amd64 golang:1.26-alpine sh -c '
  mkdir /w && tar -x -C /w && cd /w/whatsapp-bridge &&
  apk add --no-cache gcc musl-dev >&2 &&
  CGO_ENABLED=1 go build -trimpath -tags "netgo osusergo sqlite_omit_load_extension" \
    -ldflags "-s -w -linkmode external -extldflags -static" -o /tmp/whatsapp-bridge . >&2 &&
  cat /tmp/whatsapp-bridge' > dist/whatsapp-bridge
```

For an arm64 host (for example Oracle's Ampere A1), use `--platform linux/arm64`.

## 2. Prepare the host

**On the host**, create the service accounts and directories:

```bash
sudo useradd --system --home-dir /var/lib/whatsapp-mcp --create-home --shell /sbin/nologin whatsapp
sudo useradd --system --home-dir /var/lib/caddy --create-home --shell /sbin/nologin caddy
sudo chmod 750 /var/lib/whatsapp-mcp
sudo install -d -m 755 /opt/whatsapp-mcp/bin /opt/whatsapp-mcp/server /etc/whatsapp-mcp /etc/caddy
```

Code and binaries are owned by root, so the services can run them but not modify them.

Copy the bridge, the MCP server and the systemd units across:

```bash
scp dist/whatsapp-bridge deploy/systemd/*.service deploy/Caddyfile.example deploy/account.env.example <host>:/tmp/
git archive --format=tar.gz HEAD whatsapp-mcp-server | ssh <host> 'sudo tar -xz -C /opt/whatsapp-mcp/server --strip-components=1'
```

**On the host**, install them:

```bash
sudo install -m 755 /tmp/whatsapp-bridge /opt/whatsapp-mcp/bin/
sudo install -m 644 /tmp/*.service /etc/systemd/system/
sudo install -m 644 /tmp/Caddyfile.example /etc/caddy/
sudo systemctl daemon-reload
```

Install [uv](https://github.com/astral-sh/uv/releases) (the static `x86_64-unknown-linux-musl` or `aarch64-unknown-linux-musl` build, checked against its `.sha256`) to `/usr/local/bin/uv`. Then install Python 3.11 and the server's locked dependencies. This doesn't need the distribution's Python, which on Oracle Linux 9 is too old:

```bash
cd /opt/whatsapp-mcp/server
sudo env UV_PYTHON_INSTALL_DIR=/opt/whatsapp-mcp/python UV_PYTHON_PREFERENCE=only-managed \
         UV_CACHE_DIR=/var/cache/uv UV_LINK_MODE=copy \
  /usr/local/bin/uv sync --frozen --no-dev --python 3.11
sudo chmod -R a+rX /opt/whatsapp-mcp
```

On SELinux systems (Oracle Linux, RHEL, Fedora), relabel what you've installed:

```bash
sudo restorecon -R /opt/whatsapp-mcp /etc/systemd/system /usr/local/bin
```

**Optional, for voice messages.** `send_audio_message` converts other audio formats to Opus with `ffmpeg`. Without it, only files that are already `.ogg` can be sent as voice notes. Oracle Linux doesn't package ffmpeg; a static build such as [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (a stable `nX.Y` release, verified against `checksums.sha256`) installed to `/usr/local/bin/ffmpeg` works. Check it has the Opus encoder with `ffmpeg -hide_banner -encoders | grep libopus`.

## 3. Configure each account

**On the host**, for each account, create its directory and environment file:

```bash
sudo install -d -m 750 -o whatsapp -g whatsapp /var/lib/whatsapp-mcp/personal
sudo install -m 640 -o root -g whatsapp /tmp/account.env.example /etc/whatsapp-mcp/personal.env
sudoedit /etc/whatsapp-mcp/personal.env
```

In the file, replace `<account>` and `<your-domain>`, and set the token to the output of:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The MCP server refuses to start with a token shorter than 32 characters, or with a public URL that isn't `https://`.

A second account needs its own ports, and its own path in the Caddyfile. The example Caddyfile uses `personal` on 8080/9000 and `work` on 8081/9001.

## 4. HTTPS with Caddy

Install [Caddy](https://github.com/caddyserver/caddy/releases) (`caddy_<version>_linux_amd64.tar.gz`, verified against `caddy_<version>_checksums.txt`) to `/usr/local/bin/caddy`. Then, **on the host**:

```bash
sed 's/<your-domain>/your.domain.example/' /etc/caddy/Caddyfile.example | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo -u caddy caddy validate --config /etc/caddy/Caddyfile
```

Open port 443, and only port 443, in the host firewall:

```bash
sudo firewall-cmd --permanent --add-service=https && sudo firewall-cmd --reload   # firewalld
# or: sudo ufw allow 443/tcp
```

Do the same in your cloud provider's firewall, allowing **TCP 443 and 22 only**. Check what's already there: some providers' default rules allow far more than that. On OCI, the subnet's *security list* and the instance's *network security group* both apply, and traffic is allowed if **either** permits it.

Start Caddy and watch it get the certificate:

```bash
sudo systemctl enable --now caddy
sudo journalctl -u caddy -f    # wait for "certificate obtained successfully", then Ctrl+C
```

## 5. Pair each account

Have the account's phone ready. **On the host**:

```bash
sudo systemctl start whatsapp-bridge@personal
sudo journalctl -fu whatsapp-bridge@personal -o cat
```

Scan the QR code from WhatsApp → **Settings → Linked Devices → Link a Device**. When you see `✓ Connected to WhatsApp!`, press `Ctrl+C`; that only stops the log viewer, not the bridge. If a code expires before you scan it, the next one appears within seconds. Your terminal needs to be about 70 columns wide for the code to render.

To start with your existing message history, stop the bridge and copy a `messages.db` into `/var/lib/whatsapp-mcp/<account>/store/` (owner `whatsapp`, mode `600`) before pairing. Take the copy with `sqlite3 messages.db ".backup copy.db"` rather than `cp` if a bridge is still writing to it. Never copy `whatsapp.db`: it holds the device's session keys, and two bridges sharing one session can corrupt it.

## 6. Start everything and check it

**On the host**:

```bash
sudo systemctl enable --now whatsapp-bridge@personal whatsapp-mcp@personal
```

From your own computer, an unauthenticated request should be refused:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<your-domain>/personal/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}'
# 401
```

A `502` right after a restart is normal for a few seconds: on a small host the MCP server takes around 13 seconds to start listening.

## 7. Add the connector in Claude

Read the token **on the host**:

```bash
sudo grep TOKEN /etc/whatsapp-mcp/personal.env
```

In Claude, go to **Customize → Connectors → Add custom connector**:

| Field | Value |
| --- | --- |
| URL | `https://<your-domain>/personal/mcp` |
| Authentication | **No sign-in** |
| Request headers | `authorization` = `Bearer <token>` (include the word `Bearer` and a space) |

Claude will tag *Sign in now* as **Detected** and warn that the connector may fail without sign-in. That's expected: the server advertises OAuth metadata, but it authenticates with the request header instead. Choose **No sign-in** anyway.

Repeat for each account, then try something like "list my recent WhatsApp chats".

## Operating it

| Task | Command (on the host) |
| --- | --- |
| Status | `systemctl status 'whatsapp-*' caddy` |
| Logs | `sudo journalctl -fu whatsapp-mcp@personal` (or `whatsapp-bridge@…`, `caddy`) |
| Restart an account | `sudo systemctl restart whatsapp-bridge@personal whatsapp-mcp@personal` |
| Rotate a token | edit `/etc/whatsapp-mcp/<account>.env`, restart `whatsapp-mcp@<account>`, then remove and re-add the connector in Claude (header values can't be edited) |

**Updating.** From your computer, `deploy/update.sh` deploys a commit and checks it:

```bash
deploy/update.sh --host <host>                    # MCP server only, at HEAD
deploy/update.sh --host <host> --bridge           # also rebuild and restart the bridges (needs Docker)
deploy/update.sh --host <host> --units            # also reinstall the systemd units
deploy/update.sh --host <host> --ref <commit>     # deploy a specific commit
```

Set `WHATSAPP_DEPLOY_HOST` to skip `--host`. The script:

- sends only committed code, via `git archive`, so uncommitted changes and local message stores never leave your machine;
- updates every account it finds in `/etc/whatsapp-mcp/*.env`;
- health-checks each account after restarting it (MCP servers must answer `401` without a token, bridges must answer on their API port) and **rolls back** to the previous version if a check fails;
- doesn't roll back a bridge that comes up asking for a QR code, because the code is fine and the device just needs pairing again — it tells you which account;
- records what's deployed, and when, in `/opt/whatsapp-mcp/DEPLOYED`.

A full run with `--bridge` takes around two minutes on a small host, and both accounts briefly reconnect.

To do it by hand instead, unpack `git archive HEAD whatsapp-mcp-server` over `/opt/whatsapp-mcp/server`, run the `uv sync` from step 2, and restart `whatsapp-mcp@<account>`; for the bridge, rebuild it as in step 1, install it over `/opt/whatsapp-mcp/bin/whatsapp-bridge`, and restart `whatsapp-bridge@<account>`.

**When a device expires.** WhatsApp drops linked devices after roughly 20 days without a connection, or when you unlink them on the phone. The bridge then discards the dead session and prints a new QR code to its log. Follow the logs as in step 5 and scan it again; message history is kept.

**Backups.** Each account's data is in `/var/lib/whatsapp-mcp/<account>/store`. `messages.db` is your message history. `whatsapp.db` is the linked device's session, which amounts to a credential for the account: back it up as carefully as the token, or not at all and re-pair instead.

## Small hosts

On a 1 GB VM the stack uses roughly 170 MB at idle: about 20 MB per bridge, 56 MB per MCP server, and 18 MB for Caddy. The units cap each service's memory so a spike can't starve SSH. What tends to cause trouble is the operating system, not this stack:

- **Add swap** (1–2 GB) so a memory spike slows the box down instead of hanging it.
- **Don't compile on the host.** Building the bridge can exhaust memory; build it as in step 1 instead.
- **Oracle Linux 9 specifics:** by default it reserves about 450 MB for crash dumps (`crashkernel=` on the kernel command line), and its periodic `dnf-makecache` timer can push a 1 GB machine into swap thrashing. Removing the reservation (`grubby --update-kernel=ALL --remove-args=crashkernel`, plus `auto_reset_crashkernel no` in `/etc/kdump.conf` so kernel updates don't restore it) and disabling `dnf-makecache.timer` makes a large difference. If you do this, schedule security updates yourself.

## Limitations of a hosted setup

- **File paths are on the server.** `send_file` and `send_audio_message` can only send files that already exist on the host, and `download_media` saves to the host. Reading, searching and sending text work fully.
- **One shared token per account.** Anyone holding it has full access to that account through the connector. If more than one person needs access, use separate accounts or put OAuth in front of the server instead of sharing the token.

## Security notes

- **The token is the account.** Keep it out of chats, screenshots, issue trackers and version control. If it may have leaked, rotate it.
- **Your domain is part of the trust chain.** Whoever controls the DNS record can point it at their own server, get a valid certificate for it, and receive the token Claude sends. Protect the DNS provider's login and API token as carefully as the bearer token.
- **The hostname becomes public** in Certificate Transparency logs as soon as the certificate is issued, and scanners watch those logs. Pick a name that doesn't describe what's behind it; the server answers `404` for anything outside the configured paths and `401` without a valid token.
- **Prompt injection.** Messages people send you become text that Claude reads, through tools that can also send messages. See the caution about [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) in the main README, and keep tool approval on for `send_message`, `send_file` and `send_audio_message` in Claude.
- **Least privilege.** The units run as an unprivileged user with a read-only filesystem except for the account's own directory, no capabilities, and a private `/tmp`. The bridge's REST API and the MCP servers listen on loopback only; only Caddy is reachable from outside.
