"""Ship the app to the instance provision.py created, and start it.

    python deploy/deploy.py            # sync, build, start, verify
    python deploy/deploy.py --logs     # tail the app's logs
    python deploy/deploy.py --restart  # restart without rebuilding

Uses the ssh/scp already on the machine rather than an SSH library, so the
commands printed here are the same ones you would run by hand to debug it.

The build runs on a 1 GB instance and leans on the swap that user-data.sh set
up. Expect it to take a while - the frontend bundle and the LibreOffice layer
are both heavy - and expect it to be slow rather than to fail.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import boto3
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provision import KEY_PATH, NAME, find_instance, session  # noqa: E402
from ssh_window import ssh_window  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REMOTE_DIR = "/opt/app"

# What the instance needs to build and run. Everything else - node_modules,
# .venv, the local data directory, logs - is either rebuilt there or has no
# business leaving this machine.
INCLUDE = ["server", "web", "scripts", "Dockerfile", ".dockerignore", "compose.yml"]
EXCLUDE = [
    "node_modules",
    "dist",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "*.pyc",
    "*.log",
    "data",
]


def instance_ip() -> str:
    ec2 = session().client("ec2")
    inst = find_instance(ec2)
    if not inst:
        sys.exit("No instance found. Run: python deploy/provision.py")
    if inst["State"]["Name"] != "running":
        sys.exit(f"Instance is {inst['State']['Name']}, not running.")
    ip = inst.get("PublicIpAddress")
    if not ip:
        sys.exit("Instance has no public IP.")
    return ip


def hostname_for(ip: str) -> str:
    """sslip.io turns an IP into a name Let's Encrypt will issue a cert for."""
    return f"{ip.replace('.', '-')}.sslip.io"


def ssh_base(ip: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(KEY_PATH),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"ec2-user@{ip}",
    ]


def run_remote(ip: str, command: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_base(ip) + [command], check=check, text=True)


def capture_remote(ip: str, command: str) -> str:
    result = subprocess.run(
        ssh_base(ip) + [command], capture_output=True, text=True, check=False
    )
    return (result.stdout or result.stderr).strip()


def wait_for_ssh(ip: str, timeout: int = 300) -> None:
    print("  waiting for SSH ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ssh_base(ip) + ["echo ok"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            print("  ssh ready")
            return
        time.sleep(5)
    sys.exit("SSH never came up. Check the security group and the instance.")


def wait_for_bootstrap(ip: str, timeout: int = 900) -> None:
    """Block until user-data.sh finished.

    Deploying into a half-bootstrapped host is the classic way to get an error
    about `docker: command not found` and go looking in the wrong place.
    """
    print("  waiting for first-boot bootstrap (docker, swap, caddy) ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = capture_remote(
            ip,
            "test -f /var/lib/cloud/instance/bootstrap-complete && echo done || echo pending",
        )
        if out.endswith("done"):
            print("  bootstrap complete")
            return
        time.sleep(10)
    print("  ! bootstrap did not report complete; tail of its log:")
    print(capture_remote(ip, "sudo tail -30 /var/log/user-data.log"))
    sys.exit("Bootstrap timed out.")


def sync_code(ip: str) -> None:
    """Copy the repo up.

    tar over ssh rather than rsync: rsync is not present on a stock Windows
    box, while tar and ssh both are, and the exclude list keeps this to a
    couple of megabytes.
    """
    print("  syncing code ...")
    excludes = " ".join(f"--exclude='{pattern}'" for pattern in EXCLUDE)
    includes = " ".join(INCLUDE)
    # Create the tar locally and stream it straight into tar on the far end.
    local = f"tar czf - {excludes} {includes}"
    remote = f"cat | tar xzf - -C {REMOTE_DIR}"

    run_remote(ip, f"mkdir -p {REMOTE_DIR}")
    proc = subprocess.Popen(local, shell=True, cwd=ROOT, stdout=subprocess.PIPE)
    upload = subprocess.Popen(ssh_base(ip) + [remote], stdin=proc.stdout)
    if proc.stdout:
        proc.stdout.close()
    upload.communicate()
    if upload.returncode != 0:
        sys.exit("Code sync failed.")

    # The compose file and the env file are handled separately: one is
    # environment-specific, the other is secret.
    for name, dest in (
        ("deploy/compose.prod.yml", f"{REMOTE_DIR}/compose.prod.yml"),
        (".env", f"{REMOTE_DIR}/.env"),
    ):
        src = ROOT / name
        if not src.is_file():
            sys.exit(f"Missing {src}")
        subprocess.run(
            [
                "scp",
                "-i",
                str(KEY_PATH),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "LogLevel=ERROR",
                str(src),
                f"ec2-user@{ip}:{dest}",
            ],
            check=True,
        )
    # .env holds every API key the app has; keep it off other accounts.
    run_remote(ip, f"chmod 600 {REMOTE_DIR}/.env")
    print("  code and .env in place")


def configure_caddy(ip: str) -> str:
    """Point Caddy at the app and let it obtain a certificate."""
    host = hostname_for(ip)
    # Security headers are set here rather than in the app because Caddy is the
    # only thing the public reaches; the app itself binds 127.0.0.1.
    #
    # No Content-Security-Policy on purpose. The app needs WebRTC, microphone
    # access, blob workers and inline styles, and a CSP loose enough to permit
    # all of that blocks very little while being easy to get subtly wrong. The
    # headers below are the ones that cost nothing and break nothing.
    caddyfile = (
        f"{host} {{\n"
        f"\treverse_proxy 127.0.0.1:7860\n"
        f"\tencode zstd gzip\n"
        f"\n"
        f"\theader {{\n"
        # Pin browsers to HTTPS for a year. Without this a first visit over
        # http:// is downgradeable, and getUserMedia would refuse anyway.
        f'\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"\n'
        # Stop MIME sniffing turning an upload into an executable response.
        f'\t\tX-Content-Type-Options "nosniff"\n'
        # This app is never legitimately framed.
        f'\t\tX-Frame-Options "DENY"\n'
        f'\t\tReferrer-Policy "strict-origin-when-cross-origin"\n'
        # Camera and geolocation are never used; the microphone is, so it is
        # the one capability left enabled, and only for this origin.
        f'\t\tPermissions-Policy "camera=(), geolocation=(), microphone=(self)"\n'
        # Do not advertise the server software.
        f"\t\t-Server\n"
        f"\t}}\n"
        f"}}\n"
    )
    print(f"  configuring caddy for {host} ...")
    run_remote(
        ip,
        f"sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'\n{caddyfile}EOF\n"
        "sudo systemctl enable --now caddy && sudo systemctl restart caddy",
    )
    return host


def bring_up(ip: str, rebuild: bool = True) -> None:
    compose = f"cd {REMOTE_DIR} && docker compose -f compose.prod.yml"
    if rebuild:
        print("  building the image on the instance (slow on 1 GB; using swap) ...")
        run_remote(ip, f"{compose} build")
    print("  starting ...")
    run_remote(ip, f"{compose} up -d")


def verify(ip: str, host: str) -> None:
    print("\n  verifying ...")

    local_health = capture_remote(
        ip, "curl -s -m 5 http://127.0.0.1:7860/api/health || echo FAILED"
    )
    print(f"  app on the instance : {local_health[:120]}")

    url = f"https://{host}/api/health"
    print(f"  public HTTPS        : {url}")
    # Certificate issuance takes a few seconds after Caddy first starts.
    for attempt in range(12):
        try:
            r = httpx.get(url, timeout=10)
            if r.status_code == 200:
                print(f"  -> {r.status_code} {r.text[:110]}")
                print("\n  Open:", f"https://{host}")
                return
            print(f"  -> {r.status_code}; retrying")
        except Exception as exc:
            if attempt == 0:
                print("  waiting for the certificate ...")
            if attempt == 11:
                print(f"  -> still failing: {type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(10)

    print("\n  ! HTTPS did not come up. Check:")
    print(f"    ssh -i \"{KEY_PATH}\" ec2-user@{ip} 'sudo journalctl -u caddy -n 40'")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", action="store_true", help="tail app logs")
    parser.add_argument("--restart", action="store_true", help="restart, no rebuild")
    parser.add_argument("--ssh", action="store_true", help="print the ssh command")
    args = parser.parse_args()

    ip = instance_ip()
    host = hostname_for(ip)

    if args.ssh:
        print(" ".join(ssh_base(ip)))
        return

    if args.logs:
        with ssh_window():
            run_remote(
                ip,
                f"cd {REMOTE_DIR} && docker compose -f compose.prod.yml logs -f --tail=120",
                check=False,
            )
        return

    if args.restart:
        with ssh_window():
            bring_up(ip, rebuild=False)
        verify(ip, host)
        return

    print(f"Deploying to {ip} ({host})")
    # Port 22 is opened for the deploy and revoked on the way out, including
    # on failure. An IP allowlist cannot work here - see ssh_window.py for the
    # CGNAT behaviour that rules it out.
    with ssh_window():
        wait_for_ssh(ip)
        wait_for_bootstrap(ip)
        sync_code(ip)
        configure_caddy(ip)
        bring_up(ip, rebuild=True)
        verify(ip, host)


if __name__ == "__main__":
    main()
