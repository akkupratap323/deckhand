#!/bin/bash
# First-boot bootstrap for the EC2 host. Runs once, as root, via cloud-init.
#
# Output goes to /var/log/user-data.log as well as the console, because the
# single most common way to lose an afternoon here is a bootstrap that failed
# silently and an instance that looks healthy but has no Docker on it.
set -euxo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1

echo "=== bootstrap starting $(date -Is) ==="

# --- swap ------------------------------------------------------------------
# Not optional on a 1 GB box. `npm ci` plus a Vite production build plus
# `pip install` for the ML-adjacent wheel set will exhaust 1 GB and the OOM
# killer will take out the build with a message that looks nothing like "out
# of memory". 4 GB of swap turns that from a failure into merely slow.
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # The default of 60 swaps too eagerly for a build box; 10 keeps hot pages
  # resident and uses swap as the overflow it is meant to be here.
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swap.conf
fi
free -h

# --- packages --------------------------------------------------------------
dnf update -y
dnf install -y docker git tar gzip

systemctl enable --now docker
usermod -aG docker ec2-user

# Compose v2 as a CLI plugin. Pinned: an unpinned "latest" here means a
# rebuild months from now silently gets different behaviour.
COMPOSE_VERSION=v2.32.4
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# --- caddy -----------------------------------------------------------------
# Installed as a plain binary rather than a container: it is one static file,
# and on a 1 GB host not paying for a second container's runtime is worth the
# small loss of tidiness.
#
# Caddy is here for one specific reason. Browsers refuse getUserMedia on
# insecure origins, so a plain http:// deployment would load the page and then
# fail to open the microphone - the app would connect and sit silent. Caddy
# terminates TLS with a real Let's Encrypt certificate, which is what makes the
# microphone available at all.
CADDY_VERSION=2.10.0
curl -fsSL \
  "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
  -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /tmp caddy
install -m 0755 /tmp/caddy /usr/local/bin/caddy
rm -f /tmp/caddy.tar.gz /tmp/caddy

useradd --system --home /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy || true
mkdir -p /etc/caddy
# Lets Caddy bind 80/443 without running as root.
setcap cap_net_bind_service=+ep /usr/local/bin/caddy

cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy
After=network.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

mkdir -p /opt/app
chown ec2-user:ec2-user /opt/app

# A marker the deploy script polls for. Written last, so its presence means
# every step above actually completed.
touch /var/lib/cloud/instance/bootstrap-complete
echo "=== bootstrap finished $(date -Is) ==="
