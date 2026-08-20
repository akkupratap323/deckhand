/**
 * The presenter's visual centerpiece: a circular indicator that is genuinely
 * audio-reactive, not a simulated pulse.
 *
 * `VoiceVisualizer` (from @pipecat-ai/client-react) runs a real Web Audio
 * AnalyserNode against the bot's live audio track and paints bars from actual
 * frequency data onto a canvas - the bars move because the agent is speaking,
 * not because a CSS animation is guessing at it. Wrapped in a circular clip
 * with an activity-coloured glow, a spinning ring while the agent thinks, and
 * a breathing idle state, it reads as one cohesive "orb" rather than a bare
 * canvas.
 */

import { VoiceVisualizer } from "@pipecat-ai/client-react";

import type { AgentActivity } from "../types";

interface Props {
  activity: AgentActivity;
  /** Diameter in pixels. */
  size?: number;
}

// Decorative tier only - these paint directly onto the canvas, not text, so
// they use the same vivid colours as the CSS glow rather than the darker
// AA-contrast-safe tokens the rest of the UI uses for readable text.
const BAR_COLOR: Record<AgentActivity, string> = {
  idle: "#9a9aa2",
  listening: "#5b8cff",
  thinking: "#ffb347",
  speaking: "#35d68f",
};

export function VoiceOrb({ activity, size = 64 }: Props) {
  return (
    <div
      className={`orb orb--${activity}`}
      style={{ width: size, height: size }}
      role="img"
      aria-label={`Agent status: ${activity}`}
    >
      <span className="orb__glow" aria-hidden="true" />
      <span className="orb__ring" aria-hidden="true" />
      <span className="orb__visualizer" aria-hidden="true">
        <VoiceVisualizer
          participantType="bot"
          backgroundColor="transparent"
          barColor={BAR_COLOR[activity]}
          barCount={5}
          barGap={3}
          barWidth={3}
          barMaxHeight={size * 0.5}
          barOrigin="center"
          barLineCap="round"
        />
      </span>
    </div>
  );
}
