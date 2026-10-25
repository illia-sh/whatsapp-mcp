#!/usr/bin/env bash
# Update a server set up with deploy/README.md to the code at a git ref.
#
# Usage:
#   deploy/update.sh --host <ssh-destination> [--ref <git-ref>] [--bridge] [--units]
#
#   --host    SSH destination with passwordless sudo, e.g. opc@203.0.113.10.
#             Defaults to $WHATSAPP_DEPLOY_HOST.
#   --ref     Commit to deploy. Defaults to HEAD. Only committed code is sent,
#             so uncommitted changes and local message stores never leave
#             this machine.
#   --bridge  Also rebuild the Go bridge (needs Docker) and restart the bridges.
#   --units   Also reinstall the systemd units from deploy/systemd and restart
#             the bridges so their unit changes take effect.
#
# The MCP server is always updated. Accounts are discovered from
# /etc/whatsapp-mcp/*.env on the host. After each restart every account is
# health-checked; if a check fails, that component is rolled back to the
# version it replaced. A bridge that comes back asking for a QR code is not
# rolled back: the code is fine, the linked device needs pairing again.

set -euo pipefail

HOST="${WHATSAPP_DEPLOY_HOST:-}"
REF="HEAD"
BRIDGE=0
UNITS=0

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
	case "$1" in
	--host) HOST="${2:?--host needs a value}"; shift 2 ;;
	--ref) REF="${2:?--ref needs a value}"; shift 2 ;;
	--bridge) BRIDGE=1; shift ;;
	--units) UNITS=1; shift ;;
	-h | --help) usage 0 ;;
	*) echo "Unknown option: $1" >&2; usage 1 ;;
	esac
done

[ -n "$HOST" ] || { echo "No host: pass --host or set WHATSAPP_DEPLOY_HOST." >&2; exit 1; }

REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
SHA=$(git rev-parse --short "$REF^{commit}")

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	echo "Note: you have uncommitted changes. They are not deployed; $SHA is."
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

remote() { ssh -o BatchMode=yes "$HOST" "$@"; }

echo "Deploying $SHA ($(git log -1 --format=%s "$SHA")) to $HOST"

# Fail early, with a pointer, if the host isn't laid out as the guide describes.
remote 'test -d /opt/whatsapp-mcp/server/.venv && ls /etc/whatsapp-mcp/*.env >/dev/null 2>&1' || {
	echo "The host doesn't look like a deploy/README.md install (no /opt/whatsapp-mcp/server/.venv or /etc/whatsapp-mcp/*.env)." >&2
	exit 1
}

# Helpers shared by every remote step. Each step is sent as a script to
# `sudo bash -s`, so nothing here is expanded locally.
read -r -d '' COMMON <<'EOF' || true
set -euo pipefail
ROOT=/opt/whatsapp-mcp
accounts() { for f in /etc/whatsapp-mcp/*.env; do basename "$f" .env; done; }
envval() { sed -n "s/^$2=//p" "/etc/whatsapp-mcp/$1.env"; }
relabel() { if command -v restorecon >/dev/null; then restorecon -R "$@"; fi; }
record() {  # record() <component> <sha>: what is deployed, and when
	touch "$ROOT/DEPLOYED"
	sed -i "/^$1 /d" "$ROOT/DEPLOYED"
	echo "$1 $2 $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ROOT/DEPLOYED"
}
EOF

# --- systemd units ---------------------------------------------------------
if [ "$UNITS" = 1 ]; then
	echo "Installing systemd units"
	mkdir -p "$WORK/units"
	git archive "$SHA" deploy/systemd | tar -x -C "$WORK/units"
	scp -q -o BatchMode=yes "$WORK"/units/deploy/systemd/*.service "$HOST:/tmp/"
	remote 'sudo bash -s' <<EOF
$COMMON
for f in /tmp/whatsapp-bridge@.service /tmp/whatsapp-mcp@.service /tmp/caddy.service; do
	[ -f "\$f" ] && install -m 644 -o root -g root "\$f" /etc/systemd/system/ && rm -f "\$f"
done
relabel /etc/systemd/system
systemctl daemon-reload
echo "  units installed"
EOF
fi

# --- bridge ----------------------------------------------------------------
if [ "$BRIDGE" = 1 ] || [ "$UNITS" = 1 ]; then
	if [ "$BRIDGE" = 1 ]; then
		case "$(remote uname -m)" in
		x86_64) PLATFORM=linux/amd64 ;;
		aarch64 | arm64) PLATFORM=linux/arm64 ;;
		*) echo "Unsupported host architecture: $(remote uname -m)" >&2; exit 1 ;;
		esac
		echo "Building the bridge for $PLATFORM"
		git archive --format=tar "$SHA" whatsapp-bridge |
			docker run --rm -i --platform "$PLATFORM" golang:1.26-alpine sh -c '
				mkdir /w && tar -x -C /w && cd /w/whatsapp-bridge &&
				apk add --no-cache gcc musl-dev >&2 &&
				CGO_ENABLED=1 go build -trimpath -tags "netgo osusergo sqlite_omit_load_extension" \
					-ldflags "-s -w -linkmode external -extldflags -static" -o /tmp/whatsapp-bridge . >&2 &&
				cat /tmp/whatsapp-bridge' >"$WORK/whatsapp-bridge" 2>"$WORK/build.log" || {
			tail -20 "$WORK/build.log" >&2
			exit 1
		}
		scp -q -o BatchMode=yes "$WORK/whatsapp-bridge" "$HOST:/tmp/whatsapp-bridge"
	fi

	echo "Restarting the bridges"
	remote "sudo bash -s -- $SHA $BRIDGE" <<EOF
$COMMON
SHA=\$1 NEWBIN=\$2
BIN=\$ROOT/bin/whatsapp-bridge
units() { for a in \$(accounts); do echo "whatsapp-bridge@\$a"; done; }

# 0 = all up, 1 = not answering, 2 = up but waiting for a QR scan
healthy() {
	local since=\$1 status=0
	for a in \$(accounts); do
		port=\$(envval "\$a" WHATSAPP_BRIDGE_PORT) up=
		for _ in \$(seq 1 30); do
			if curl -s -m 3 -X POST "http://127.0.0.1:\$port/api/send" -d '{}' | grep -q "Recipient is required"; then up=1; break; fi
			if journalctl -u "whatsapp-bridge@\$a" --since "\$since" --no-pager -o cat | grep -q "Scan this QR code"; then break; fi
			sleep 2
		done
		if [ -n "\$up" ]; then
			echo "  \$a: bridge connected, API up on :\$port"
		elif journalctl -u "whatsapp-bridge@\$a" --since "\$since" --no-pager -o cat | grep -q "Scan this QR code"; then
			echo "  \$a: bridge is running but needs pairing again: journalctl -fu whatsapp-bridge@\$a -o cat"
			status=2
		else
			echo "  \$a: bridge not answering on :\$port"
			return 1
		fi
	done
	return \$status
}

if [ "\$NEWBIN" = 1 ]; then
	install -m 755 -o root -g root /tmp/whatsapp-bridge "\$BIN.new" && rm -f /tmp/whatsapp-bridge
	cp -p "\$BIN" "\$BIN.prev"
	mv "\$BIN.new" "\$BIN"
	relabel "\$ROOT/bin"
fi

since=\$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart \$(units)
set +e; healthy "\$since"; rc=\$?; set -e
if [ \$rc = 0 ] || [ \$rc = 2 ]; then
	[ "\$NEWBIN" = 1 ] && record bridge "\$SHA"
	[ \$rc = 2 ] && echo "  (code deployed; pair the devices above to finish)"
	exit 0
fi
if [ "\$NEWBIN" = 1 ]; then
	echo "  rolling the bridge back"
	mv "\$BIN.prev" "\$BIN"
	relabel "\$ROOT/bin"
	since=\$(date '+%Y-%m-%d %H:%M:%S')
	systemctl restart \$(units)
	healthy "\$since" || true
fi
exit 1
EOF
fi

# --- MCP server --------------------------------------------------------------
echo "Updating the MCP server"
git archive --format=tar.gz -o "$WORK/server.tar.gz" "$SHA" whatsapp-mcp-server
scp -q -o BatchMode=yes "$WORK/server.tar.gz" "$HOST:/tmp/whatsapp-mcp-server.tar.gz"
remote "sudo bash -s -- $SHA" <<EOF
$COMMON
SHA=\$1
units() { for a in \$(accounts); do echo "whatsapp-mcp@\$a"; done; }

# Python and the locked dependencies, into the existing virtualenv.
sync_deps() {
	(cd "\$ROOT/server" && env UV_PYTHON_INSTALL_DIR="\$ROOT/python" UV_PYTHON_PREFERENCE=only-managed \
		UV_CACHE_DIR=/var/cache/uv UV_LINK_MODE=copy \
		/usr/local/bin/uv sync --frozen --no-dev --python 3.11 --quiet)
	chmod -R a+rX "\$ROOT/server"
	relabel "\$ROOT/server"
}

# Every account's server must answer 401 without a token: up, and enforcing auth.
healthy() {
	for a in \$(accounts); do
		port=\$(envval "\$a" WHATSAPP_MCP_PORT) code=
		for _ in \$(seq 1 30); do
			code=\$(curl -s -m 3 -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:\$port/mcp" \
				-H 'Content-Type: application/json' -d '{}' || true)
			[ "\$code" = 401 ] && break
			sleep 2
		done
		if [ "\$code" = 401 ]; then echo "  \$a: MCP server up on :\$port"; else echo "  \$a: MCP server not answering on :\$port (last: \${code:-none})"; return 1; fi
	done
}

# Unpack beside the live copy, carry the virtualenv over, then swap. The
# replaced copy is kept as server.prev for rollback.
rm -rf "\$ROOT/server.new" && mkdir "\$ROOT/server.new"
tar -xzf /tmp/whatsapp-mcp-server.tar.gz -C "\$ROOT/server.new" --strip-components=1 && rm -f /tmp/whatsapp-mcp-server.tar.gz
mv "\$ROOT/server/.venv" "\$ROOT/server.new/.venv"
rm -rf "\$ROOT/server.prev"
mv "\$ROOT/server" "\$ROOT/server.prev"
mv "\$ROOT/server.new" "\$ROOT/server"
sync_deps
systemctl restart \$(units)
if healthy; then
	record server "\$SHA"
	exit 0
fi

echo "  rolling the MCP server back"
rm -rf "\$ROOT/server.failed"
mv "\$ROOT/server" "\$ROOT/server.failed"
mv "\$ROOT/server.prev" "\$ROOT/server"
mv "\$ROOT/server.failed/.venv" "\$ROOT/server/.venv"
sync_deps
systemctl restart \$(units)
healthy || true
echo "  the failed copy is in \$ROOT/server.failed; logs: journalctl -u 'whatsapp-mcp@*'"
exit 1
EOF

echo "Done. Deployed versions on the host:"
remote 'cat /opt/whatsapp-mcp/DEPLOYED' | sed 's/^/  /'
