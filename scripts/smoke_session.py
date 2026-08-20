"""Headless smoke test of the full voice pipeline - no browser, no microphone.

Connects to /api/offer exactly as the browser would, streams silence, and
reports what comes back: ICE state, whether the bot's audio track produced
frames, and which RTVI messages arrived on the data channel.

This is how the pipeline gets verified without a microphone. A PASS means
signalling, ICE (including through Docker's NAT), Deepgram Flux STT, DeepSeek,
Deepgram Flux TTS and the RTVI slide channel are all working end to end.

    python scripts/smoke_session.py
    python scripts/smoke_session.py --deck <deck_id> --seconds 45

Needs the server's virtualenv - aiortc, aiohttp, numpy and av all come with the
backend requirements:

    server/.venv/Scripts/python scripts/smoke_session.py     # Windows
    server/.venv/bin/python scripts/smoke_session.py         # macOS / Linux
"""

import argparse
import asyncio
import socket
import fractions
import json
import time

import aiohttp
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

SAMPLE_RATE = 16000
SAMPLES = 320  # 20ms


class SilenceTrack(MediaStreamTrack):
    """Emits 20ms frames of silence, paced in real time."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._pts = 0
        self._start = time.time()

    async def recv(self):
        target = self._start + (self._pts / SAMPLE_RATE)
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame = AudioFrame.from_ndarray(
            np.zeros((1, SAMPLES), dtype=np.int16), format="s16", layout="mono"
        )
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += SAMPLES
        return frame


async def main(base_url: str, seconds: int, deck_id: str | None, presenter: str | None = None):
    pc = RTCPeerConnection(
        RTCConfiguration([RTCIceServer(urls="stun:stun.l.google.com:19302")])
    )

    audio_frames = 0
    audio_bytes = 0
    # Frames carrying actual sound, not silence. Counting frames alone is
    # not enough: WebRTC keeps sending them when the bot is saying nothing,
    # so a dead TTS still produced a full stream of quiet frames and the
    # old verdict passed on it.
    voiced_frames = 0
    peak_amplitude = 0
    messages: list[dict] = []
    got_track = asyncio.Event()

    @pc.on("connectionstatechange")
    async def on_state():
        print(f"  [pc] connection state -> {pc.connectionState}")

    @pc.on("iceconnectionstatechange")
    async def on_ice():
        print(f"  [pc] ICE state -> {pc.iceConnectionState}")

    @pc.on("track")
    def on_track(track):
        print(f"  [pc] received {track.kind} track from the bot")
        got_track.set()

        async def drain():
            nonlocal audio_frames, audio_bytes, voiced_frames, peak_amplitude
            while True:
                try:
                    frame = await track.recv()
                except Exception:
                    return
                audio_frames += 1
                audio_bytes += frame.samples * 2
                try:
                    samples = frame.to_ndarray()
                    loudest = int(abs(samples).max())
                    peak_amplitude = max(peak_amplitude, loudest)
                    # 16-bit PCM: a comfort-noise floor sits far below this.
                    if loudest > 500:
                        voiced_frames += 1
                except Exception:
                    # Never let frame inspection kill the drain loop.
                    pass

        asyncio.ensure_future(drain())

    channel = pc.createDataChannel("rtvi")

    @channel.on("open")
    def on_open():
        print("  [dc] data channel open; sending client-ready")
        channel.send(json.dumps({
            "id": "1", "label": "rtvi-ai", "type": "client-ready",
            "data": {"version": "1.0.0"},
        }))

    @channel.on("message")
    def on_message(raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        messages.append(msg)
        t = msg.get("type")
        if t == "server-message":
            inner = msg.get("data", {})
            print(f"  [dc] server-message: {json.dumps(inner)[:160]}")
        elif t in ("bot-ready", "error", "error-response"):
            print(f"  [dc] {t}: {json.dumps(msg.get('data', {}))[:200]}")

    pc.addTrack(SilenceTrack())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    params = []
    if deck_id:
        params.append(f"deck_id={deck_id}")
    if presenter:
        params.append(f"presenter={presenter}")
    offer_url = f"{base_url}/api/offer" + ("?" + "&".join(params) if params else "")
    print(f"POST {offer_url}")
    # Force IPv4, for the same reason server/net.py does: on a DNS64 and NAT64
    # network the synthetic IPv6 address is returned first and never answers,
    # so the signalling POST hangs until it times out.
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(
            offer_url,
            json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            print(f"  signalling: HTTP {resp.status}")
            answer = await resp.json()

    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
    )
    print(f"  pc_id: {answer.get('pc_id')}")

    try:
        await asyncio.wait_for(got_track.wait(), timeout=25)
    except asyncio.TimeoutError:
        print("  !! no media track from the bot within 25s")

    print(f"\nListening for {seconds}s...\n")
    await asyncio.sleep(seconds)

    print("\n===== RESULT =====")
    print(f"ICE state          : {pc.iceConnectionState}")
    print(f"Connection state   : {pc.connectionState}")
    print(f"Audio frames rcvd  : {audio_frames}")
    print(f"Audio bytes rcvd   : {audio_bytes:,}")
    print(f"Voiced frames      : {voiced_frames} (peak amplitude {peak_amplitude})")
    print(f"RTVI messages      : {len(messages)}")

    kinds: dict[str, int] = {}
    for m in messages:
        kinds[m.get("type", "?")] = kinds.get(m.get("type", "?"), 0) + 1
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"   {k}: {v}")

    server_msgs = [
        m.get("data", {}).get("data", m.get("data"))
        for m in messages
        if m.get("type") == "server-message"
    ]
    if server_msgs:
        print("\nServer messages (the slide channel):")
        for sm in server_msgs:
            print(f"   {json.dumps(sm)[:220]}")

    # Three independent conditions, because each catches a failure the others
    # miss: no media path at all, a media path carrying only silence, and a
    # pipeline that never attempted speech.
    #
    # The silence check earns its place. When Cartesia began answering 402
    # ("Model credits limit reached") the transport still delivered a full
    # stream of quiet frames, and a verdict of `audio_frames > 0` reported
    # PASS on a completely mute agent.
    spoke = kinds.get("bot-started-speaking", 0) > 0
    reasons = []
    if audio_frames == 0:
        reasons.append("no audio frames received")
    if voiced_frames == 0:
        reasons.append(f"audio carried only silence (peak amplitude {peak_amplitude})")
    if not spoke:
        reasons.append("bot never started speaking (TTS likely failed)")

    verdict = "PASS" if not reasons else "FAIL"
    print(f"\nAudio round trip: {verdict}")
    for reason in reasons:
        print(f"  - {reason}")

    await pc.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:7860")
    p.add_argument("--presenter", default=None, help="presenter id, e.g. briefer")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--deck", default=None)
    a = p.parse_args()
    asyncio.run(main(a.url, a.seconds, a.deck, a.presenter))
