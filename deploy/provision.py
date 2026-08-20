"""Create the AWS resources this app needs, and launch the instance.

Run from the repo root:

    python deploy/provision.py            # create everything, print the IP
    python deploy/provision.py --status   # what exists right now
    python deploy/provision.py --destroy  # remove it all again

Everything it creates is tagged and named, and every step is idempotent:
running it twice finds what already exists instead of making a second copy.
Credentials come from the repo's .env, the same ones the vision path uses.

Why the security group looks the way it does
--------------------------------------------
WebRTC media does not travel over the HTTPS port. aiortc binds ephemeral UDP
ports chosen at runtime and there is no supported way to pin them to a narrow
range, so the whole ephemeral UDP range has to be reachable or audio silently
never flows while signalling appears to succeed. TCP is narrow by comparison:
22 for SSH (locked to the IP running this script), 80 and 443 for the app and
for the ACME challenge that issues its certificate.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

NAME = "voice-presentation-agent"
KEY_NAME = f"{NAME}-key"
SG_NAME = f"{NAME}-sg"
INSTANCE_TYPE = os.environ.get("EC2_INSTANCE_TYPE", "t3.micro")
KEY_PATH = ROOT / "deploy" / f"{KEY_NAME}.pem"

# Amazon Linux 2023, resolved from the public SSM parameter so the id is
# always current and correct for the region rather than a stale constant.
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

TAGS = [{"Key": "Name", "Value": NAME}, {"Key": "ManagedBy", "Value": "provision.py"}]


def session() -> boto3.Session:
    """A boto3 session from the repo's .env, tolerating either key casing."""
    key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("aws_access_key_id")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get(
        "aws_secret_access_key"
    )
    if not key or not secret:
        sys.exit("No AWS credentials in .env (need AWS_ACCESS_KEY_ID/SECRET).")
    return boto3.Session(
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def my_ip() -> str:
    """This machine's public IP, for the SSH rule.

    Falls back to open SSH only if the lookup fails, and says so loudly - a
    silent 0.0.0.0/0 on port 22 is exactly the kind of thing that should never
    happen without the operator reading a line about it.
    """
    try:
        ip = httpx.get("https://checkip.amazonaws.com", timeout=10).text.strip()
        if ip:
            return f"{ip}/32"
    except Exception:
        pass
    print("  ! could not determine your public IP; opening SSH to the world")
    return "0.0.0.0/0"


# --- key pair --------------------------------------------------------------


def ensure_key_pair(ec2) -> None:
    """Create the key pair, or confirm the one we already hold still matches."""
    try:
        ec2.describe_key_pairs(KeyNames=[KEY_NAME])
        if not KEY_PATH.is_file():
            sys.exit(
                f"AWS has a key pair named {KEY_NAME} but {KEY_PATH} is missing.\n"
                "The private key cannot be re-downloaded. Delete the AWS key pair "
                "and re-run, or restore the .pem."
            )
        print(f"  key pair {KEY_NAME} (existing)")
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise

    created = ec2.create_key_pair(KeyName=KEY_NAME, KeyType="ed25519")
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Binary write, deliberately. `write_text` on Windows translates every "\n"
    # to "\r\n", and a private key with CRLF line endings is rejected by ssh
    # with "error in libcrypto" - which reads like a corrupt key rather than a
    # line-ending problem and sends you looking in entirely the wrong place.
    KEY_PATH.write_bytes(created["KeyMaterial"].encode())
    # OpenSSH refuses a key any other account can read. On Windows the POSIX
    # bit is advisory, hence the note rather than a hard guarantee.
    try:
        KEY_PATH.chmod(0o600)
    except OSError:
        pass
    print(f"  key pair {KEY_NAME} created -> {KEY_PATH}")


# --- security group --------------------------------------------------------


def ensure_security_group(ec2) -> str:
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [SG_NAME]}]
    )["SecurityGroups"]
    if existing:
        sg_id = existing[0]["GroupId"]
        print(f"  security group {SG_NAME} (existing, {sg_id})")
        return sg_id

    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])[
        "Vpcs"
    ][0]["VpcId"]

    sg_id = ec2.create_security_group(
        GroupName=SG_NAME,
        Description="Voice presentation agent: HTTPS, SSH, and WebRTC media",
        VpcId=vpc_id,
    )["GroupId"]

    ssh_cidr = my_ip()
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH from operator"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP + ACME"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}],
            },
            {
                # See the module docstring: aiortc picks these at runtime and
                # they cannot be pinned, so the range has to be open or media
                # never flows.
                "IpProtocol": "udp",
                "FromPort": 1024,
                "ToPort": 65535,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "WebRTC media"}],
            },
        ],
    )
    print(f"  security group {SG_NAME} created ({sg_id}); SSH limited to {ssh_cidr}")
    return sg_id


# --- instance --------------------------------------------------------------


def find_instance(ec2) -> dict | None:
    """The one instance this script manages, if it is alive."""
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]},
        ]
    )["Reservations"]
    for r in reservations:
        for inst in r["Instances"]:
            return inst
    return None


def launch(ec2, ssm, sg_id: str) -> dict:
    ami = ssm.get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
    user_data = (ROOT / "deploy" / "user-data.sh").read_text()

    print(f"  launching {INSTANCE_TYPE} from {ami} ...")
    result = ec2.run_instances(
        ImageId=ami,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        KeyName=KEY_NAME,
        SecurityGroupIds=[sg_id],
        UserData=user_data,
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    # The image alone is ~2.2 GB once LibreOffice is in it, and
                    # a build needs room for layers on top. 8 GB (the default)
                    # runs out partway through and fails confusingly.
                    "VolumeSize": 20,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                },
            }
        ],
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": TAGS},
            {"ResourceType": "volume", "Tags": TAGS},
        ],
        MetadataOptions={"HttpTokens": "required"},
    )
    return result["Instances"][0]


def wait_running(ec2, instance_id: str) -> dict:
    print("  waiting for the instance to run ...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
        "Instances"
    ][0]
    print("  waiting for status checks (this is the slow part) ...")
    ec2.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id])
    return inst


# --- commands --------------------------------------------------------------


def cmd_create() -> None:
    s = session()
    ec2, ssm = s.client("ec2"), s.client("ssm")
    print(f"Region {s.region_name}")

    ensure_key_pair(ec2)
    sg_id = ensure_security_group(ec2)

    inst = find_instance(ec2)
    if inst:
        print(f"  instance {inst['InstanceId']} already exists ({inst['State']['Name']})")
    else:
        inst = launch(ec2, ssm, sg_id)

    inst = wait_running(ec2, inst["InstanceId"])
    ip = inst.get("PublicIpAddress")

    # sslip.io resolves <dashed-ip>.sslip.io to that IP, which gives Let's
    # Encrypt a real hostname to issue against. Without a hostname there is no
    # certificate, and without a certificate the browser will not hand over a
    # microphone. Buying a domain is the only other way out of that.
    host = f"{ip.replace('.', '-')}.sslip.io" if ip else None

    print()
    print("  instance :", inst["InstanceId"])
    print("  public IP:", ip)
    print("  hostname :", host)
    print("  key      :", KEY_PATH)
    print()
    print("  ssh -i", f'"{KEY_PATH}"', f"ec2-user@{ip}")
    print()
    print("  Next: python deploy/deploy.py")


def cmd_status() -> None:
    s = session()
    ec2 = s.client("ec2")
    inst = find_instance(ec2)
    if not inst:
        print("No instance.")
        return
    ip = inst.get("PublicIpAddress")
    print(f"  {inst['InstanceId']}  {inst['State']['Name']}  {inst['InstanceType']}")
    print(f"  public IP: {ip}")
    if ip:
        print(f"  https://{ip.replace('.', '-')}.sslip.io")


def cmd_destroy() -> None:
    s = session()
    ec2 = s.client("ec2")

    inst = find_instance(ec2)
    if inst:
        iid = inst["InstanceId"]
        print(f"  terminating {iid} ...")
        ec2.terminate_instances(InstanceIds=[iid])
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[iid])
        print("  terminated")

    # The security group cannot be deleted while an ENI still references it,
    # which lingers briefly after termination.
    for attempt in range(10):
        try:
            groups = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [SG_NAME]}]
            )["SecurityGroups"]
            if groups:
                ec2.delete_security_group(GroupId=groups[0]["GroupId"])
                print(f"  deleted security group {SG_NAME}")
            break
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "DependencyViolation" and attempt < 9:
                time.sleep(6)
                continue
            print(f"  ! security group: {exc.response['Error']['Message'][:120]}")
            break

    try:
        ec2.delete_key_pair(KeyName=KEY_NAME)
        print(f"  deleted key pair {KEY_NAME}")
    except ClientError as exc:
        print(f"  ! key pair: {exc.response['Error']['Code']}")
    if KEY_PATH.is_file():
        KEY_PATH.unlink()
        print(f"  removed {KEY_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="show what exists")
    parser.add_argument("--destroy", action="store_true", help="remove everything")
    args = parser.parse_args()

    if args.destroy:
        cmd_destroy()
    elif args.status:
        cmd_status()
    else:
        cmd_create()


if __name__ == "__main__":
    main()
