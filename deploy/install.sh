#!/usr/bin/env bash
# One-shot deploy of Reply Studio from GitHub onto the ops VM (Ubuntu 24.04).
# Run ON the VM as root:
#   curl -sL https://raw.githubusercontent.com/nathanielsai19-rgb/reply-studio/main/deploy/install.sh | bash
set -e

apt-get update -y
apt-get install -y git ffmpeg python3-venv python3-pip

rm -rf /opt/reply-studio
git clone https://github.com/nathanielsai19-rgb/reply-studio.git /opt/reply-studio
cd /opt/reply-studio

python3 -m venv .venv
.venv/bin/pip install -q flask requests pillow

cp deploy/reply-studio.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reply-studio

echo
echo "Deployed. Remaining manual steps:"
echo "  1. Put the Groq key on the VM: scp groq_key.txt root@<vm>:/opt/reply-studio/groq_key.txt"
echo "  2. tailscale up on the VM, then put its 100.x IP in the service:"
echo "     sed -i 's|Environment=REPLY_HOST=.*|Environment=REPLY_HOST=<100.x.y.z>|' /etc/systemd/system/reply-studio.service"
echo "     systemctl daemon-reload && systemctl restart reply-studio"
echo "  3. Check: systemctl status reply-studio"
