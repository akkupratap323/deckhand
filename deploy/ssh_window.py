"""Open SSH only for as long as a deploy needs it, then close it again.

Why this exists rather than a security-group rule pinned to an IP: the
operator's connection is behind Reliance Jio's CGNAT, which hands out a
different public address per flow and rotates across unrelated ranges -
``47.15.101.95``, ``47.15.82.19`` and ``49.36.243.113`` were all observed from
the same machine within an hour, spanning two different /8s. There is no CIDR
short of "most of the internet" that reliably admits that machine and nothing
else, so an allowlist is the wrong tool.

What is safe here is the *duration*. Port 22 is open for the minutes a deploy
takes and closed immediately afterwards, including when the deploy fails. The
instance accepts keys only - ``passwordauthentication no`` and
``kbdinteractiveauthentication no``, verified on the running host - so the
exposure during that window is an ed25519 keypair, not a password guess.

Used as a context manager, so the rule is revoked on the way out whatever
happens::

    with ssh_window(ip):
        ...deploy...

If the process is killed hard enough to skip the ``finally`` (SIGKILL, a lost
laptop), the rule stays. ``python deploy/ssh_window.py --close`` removes it.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provision import SG_NAME, session  # noqa: E402

OPEN_CIDR = "0.0.0.0/0"
DESCRIPTION = "deploy window - revoked when the deploy finishes"


def _group_id(ec2) -> str:
    groups = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [SG_NAME]}]
    )["SecurityGroups"]
    if not groups:
        sys.exit(f"No security group named {SG_NAME}. Run provision.py first.")
    return groups[0]["GroupId"]


def _permission() -> list[dict]:
    return [
        {
            "IpProtocol": "tcp",
            "FromPort": 22,
            "ToPort": 22,
            "IpRanges": [{"CidrIp": OPEN_CIDR, "Description": DESCRIPTION}],
        }
    ]


def open_ssh(ec2, group_id: str) -> None:
    try:
        ec2.authorize_security_group_ingress(
            GroupId=group_id, IpPermissions=_permission()
        )
        print("  ssh window: opened port 22")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidPermission.Duplicate":
            print("  ssh window: port 22 already open")
        else:
            raise


def close_ssh(ec2, group_id: str) -> None:
    try:
        ec2.revoke_security_group_ingress(
            GroupId=group_id, IpPermissions=_permission()
        )
        print("  ssh window: closed port 22")
    except ClientError as exc:
        # Already gone is the desired end state, not a problem.
        if exc.response["Error"]["Code"] != "InvalidPermission.NotFound":
            print(f"  ! could not close port 22: {exc.response['Error']['Code']}")


@contextmanager
def ssh_window():
    """Open port 22 for the duration of the block, then revoke it."""
    ec2 = session().client("ec2")
    group_id = _group_id(ec2)
    open_ssh(ec2, group_id)
    try:
        yield
    finally:
        close_ssh(ec2, group_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true", help="open port 22")
    parser.add_argument("--close", action="store_true", help="close port 22")
    parser.add_argument("--status", action="store_true", help="who can reach port 22")
    args = parser.parse_args()

    ec2 = session().client("ec2")
    group_id = _group_id(ec2)

    if args.open:
        open_ssh(ec2, group_id)
    elif args.close:
        close_ssh(ec2, group_id)
    else:
        group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
        rules = [
            r["CidrIp"]
            for p in group["IpPermissions"]
            if p.get("FromPort") == 22
            for r in p["IpRanges"]
        ]
        print(f"  port 22 open to: {rules or 'nobody'}")


if __name__ == "__main__":
    main()
