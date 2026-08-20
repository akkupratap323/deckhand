/**
 * Connect / disconnect, mic mute, bot audio mute, slide position, and the
 * activity indicator.
 *
 * The activity indicator is worth more than it looks during a demo: it is the
 * visible proof that barge-in worked, because it flips off the moment the agent
 * is interrupted rather than a beat later. The orb carries the same signal
 * visually (and, while the agent talks, from real audio data) - the text pill
 * stays alongside it for an accessible, always-legible status.
 */

import { usePipecatClientMicControl } from "@pipecat-ai/client-react";

import type { AgentActivity } from "../types";
import { VoiceOrb } from "./VoiceOrb";

interface Props {
  activity: AgentActivity;
  connected: boolean;
  connecting: boolean;
  current: number;
  total: number;
  onConnect: () => void;
  onDisconnect: () => void;
  /** False while the agent's audio track is disabled. */
  botAudioOn: boolean;
  onToggleBotAudio: () => void;
}

const ACTIVITY_LABEL: Record<AgentActivity, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

export function Controls({
  activity,
  connected,
  connecting,
  current,
  total,
  onConnect,
  onDisconnect,
  botAudioOn,
  onToggleBotAudio,
}: Props) {
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();

  return (
    <div className="controls">
      <VoiceOrb activity={activity} size={52} />

      <div className="controls__left">
        <button
          className={`btn btn--primary${connected ? " btn--danger" : ""}`}
          onClick={connected ? onDisconnect : onConnect}
          disabled={connecting}
        >
          {connecting
            ? "Connecting…"
            : connected
              ? "End presentation"
              : "Start presentation"}
        </button>

        <button
          className="btn"
          onClick={() => enableMic(!isMicEnabled)}
          disabled={!connected}
          aria-pressed={!isMicEnabled}
        >
          {isMicEnabled ? "Mute mic" : "Unmute mic"}
        </button>

        {/* Silences the agent without ending the session or stopping the
            pipeline - the transcript and slide navigation keep working, which
            is the point: this is "stop talking", not "stop presenting". */}
        <button
          className="btn"
          onClick={onToggleBotAudio}
          disabled={!connected}
          aria-pressed={!botAudioOn}
          title={
            botAudioOn
              ? "Silence the agent's voice (the session keeps running)"
              : "Let the agent be heard again"
          }
        >
          {botAudioOn ? "Stop voice" : "Resume voice"}
        </button>
      </div>

      <div className="controls__right">
        <span
          className={`activity activity--${activity}`}
          aria-live="polite"
          aria-label={`Agent status: ${ACTIVITY_LABEL[activity]}`}
        >
          <span className="activity__dot" aria-hidden="true" />
          {ACTIVITY_LABEL[activity]}
        </span>

        {total > 0 && (
          <span className="counter" aria-label={`Slide ${current} of ${total}`}>
            {current} / {total}
          </span>
        )}
      </div>
    </div>
  );
}
