#!/bin/bash
set -e

FERRARI_IP="100.70.110.7"
FERRARI_PORT="8099"

echo "=== Installing Antigravity Remote Tailscale Client ==="
echo "Target Hub: ferrari ($FERRARI_IP:$FERRARI_PORT)"

BIN_DIR="/usr/local/bin"
if [ ! -w "$BIN_DIR" ]; then
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

cat <<'CLIENT_SCRIPT' > "$BIN_DIR/agy-manager"
#!/bin/bash
HUB_HOST="100.70.110.7"
HUB_URL="http://100.70.110.7:8099"

if [ "$1" = "web" ]; then
    echo "Opening Web Dashboard: $HUB_URL"
    xdg-open "$HUB_URL" 2>/dev/null || open "$HUB_URL" 2>/dev/null || echo "Visit: $HUB_URL"
    exit 0
fi

if [ -z "$1" ]; then
    ssh -t kacper@$HUB_HOST "agy-manager --help"
    exit 0
fi

# Execute on hub with full TTY
ssh -t kacper@$HUB_HOST "agy-manager $@"
CLIENT_SCRIPT

chmod +x "$BIN_DIR/agy-manager"

echo "✓ agy-manager client installed to $BIN_DIR/agy-manager"
echo "You can now run 'agy-manager profiles', 'agy-manager usage', or 'agy-manager attach <profile>' from this machine!"
echo "Web Dashboard is also live at: http://$FERRARI_IP:$FERRARI_PORT"
