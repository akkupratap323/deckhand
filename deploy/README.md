# Deploying to EC2

One `t3.micro` running the app in Docker behind Caddy, which terminates TLS.

```
python deploy/provision.py     # key pair, security group, instance
python deploy/deploy.py        # sync code, build, start, verify
```

Then open the `https://<dashed-ip>.sslip.io` URL it prints.

Day-to-day:

```
python deploy/deploy.py            # redeploy after code changes
python deploy/deploy.py --logs     # tail application logs
python deploy/deploy.py --restart  # restart without rebuilding
python deploy/deploy.py --ssh      # print the ssh command
python deploy/provision.py --status
python deploy/provision.py --destroy   # remove everything
```

## Why it is shaped this way

Four constraints drove every decision here, and none of them are obvious until
something breaks.

**HTTPS is mandatory, not a nicety.** Browsers refuse `getUserMedia` on an
insecure origin. Served over plain `http://`, the page loads, the WebRTC
session connects, and the microphone never opens - the app looks like it is
running and is silently deaf. Caddy therefore terminates TLS with a real Let's
Encrypt certificate, and the app itself binds `127.0.0.1` so it is never
reachable except through it.

**The certificate needs a hostname, and an IP is not one.** Let's Encrypt will
not issue for a bare IP address. Rather than require a purchased domain,
`sslip.io` is used as wildcard DNS: `98-80-186-155.sslip.io` resolves to
`98.80.186.155`, which is a real name ACME will validate. Point a real domain
at the instance and change the `Caddyfile` if you would rather not depend on a
third-party resolver.

**WebRTC media does not use the HTTPS port.** aiortc binds ephemeral UDP ports
it chooses at runtime, and there is no supported way to pin them, so the
security group opens UDP 1024-65535. Without that, signalling succeeds over
443 and audio never flows - the failure looks like a broken agent rather than
a firewall.

The compose file also uses `network_mode: host`. Bridge networking works
locally because ICE is bidirectional - the browser's candidates are routable
from the container even though the reverse is not true. On EC2 that reasoning
collapses: the browser is on the public internet with no route back to a
`172.17.x.x` candidate. Host networking puts aiortc on the real interface, and
the STUN server configured in `main.py` discovers the public address and
advertises it as a server-reflexive candidate.

**1 GB of RAM cannot build this image unaided.** `npm ci`, a Vite production
build, and `pip install` over the ML-adjacent wheel set will exhaust 1 GB, and
the OOM killer's message looks nothing like "out of memory". `user-data.sh`
creates 4 GB of swap with `vm.swappiness=10` before anything else happens.
The build then takes a long time rather than failing. The container is capped
at `mem_limit: 800m` so that if it does misbehave it restarts instead of
dragging the instance down.

Measured for reference: the app idles at ~266 MB RSS; the image is ~2.2 GB with
LibreOffice included, which is why the root volume is 20 GB rather than the
8 GB default.

## No GPU

Every model this app uses is a remote API - Deepgram for STT, Cartesia for TTS,
DeepSeek for the LLM, Gemini for vision narration. The only inference library
present is `onnxruntime`, which is CPU-only and never constructed, since
Deepgram Flux owns turn detection and the Silero VAD analyzer is deliberately
not used. A GPU instance would cost roughly $380/month and change nothing.

## Secrets

`.env` is copied to the instance over `scp` and `chmod 600`, and is listed in
`.dockerignore` so it is never baked into an image layer. `deploy/*.pem` is
gitignored; AWS cannot re-issue that private key, so losing it means
recreating the key pair.

The security group limits SSH to the public IP of whichever machine ran
`provision.py`. If your address changes, update the rule or re-run after
deleting the group.

## Cost

`t3.micro` is free-tier eligible for 750 hours/month for the first 12 months.
Outside that it is roughly $7.50/month, plus about $1.60/month for the 20 GB
gp3 volume, plus egress. `provision.py --destroy` removes the instance, the
security group, and the key pair.
