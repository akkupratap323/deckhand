/**
 * A caption that lands one word at a time, in step with the voice.
 *
 * The sync is real, not simulated. Cartesia returns per-word timestamps with
 * its audio; Pipecat consumes those server-side to release one `TTSTextFrame`
 * per word *in playback order*, and each of those arrives here as a
 * `BotTtsText` event. So a word showing up in this component means that word
 * is being spoken right now - no timers, no estimated words-per-minute, and
 * nothing that can drift out of step with the audio.
 *
 * What the event does not carry is the timestamp itself: `BotTTSTextData` is
 * `{ text }` only. That is enough for this - arrival order is the timing - but
 * it does mean the component cannot look ahead, which is why the "current"
 * word is simply the last one to arrive rather than something scheduled.
 */

import { RTVIEvent } from "@pipecat-ai/client-js";
import type { BotTTSTextData } from "@pipecat-ai/client-js";
import { useRTVIClientEvent } from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";

/** Words kept on screen. Older ones scroll off rather than growing forever. */
const WINDOW = 44;

interface Props {
  /** Hidden entirely when the session is not live. */
  active: boolean;
  /**
   * The agent's voice is silenced. Subtitles keep running - this is exactly
   * when reading them matters, since the text is now the only output. The
   * panel says so rather than looking broken.
   */
  muted?: boolean;
}

export function LiveCaption({ active, muted }: Props) {
  const [words, setWords] = useState<string[]>([]);
  const lineRef = useRef<HTMLParagraphElement>(null);

  useRTVIClientEvent(
    RTVIEvent.BotTtsText,
    useCallback((data: BotTTSTextData) => {
      const text = data?.text ?? "";
      if (!text.trim()) return;
      setWords((prev) => {
        const next = [...prev, text.trim()];
        return next.length > WINDOW ? next.slice(next.length - WINDOW) : next;
      });
    }, []),
  );

  // A new utterance starts a new caption; otherwise the previous answer's
  // tail sits in front of the new one.
  useRTVIClientEvent(
    RTVIEvent.BotTtsStarted,
    useCallback(() => setWords([]), []),
  );

  useRTVIClientEvent(
    RTVIEvent.UserStartedSpeaking,
    useCallback(() => setWords([]), []),
  );

  // Keep the newest word in view by scrolling this element only.
  //
  // This fired scrollIntoView on a tail sentinel, once per word - and since
  // scrollIntoView walks every scrollable ancestor, the page itself jumped on
  // each word of every sentence. Setting scrollTop is confined to this box.
  useEffect(() => {
    const box = lineRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [words]);

  if (!active || words.length === 0) return null;

  const last = words.length - 1;

  return (
    <div className="caption" data-muted={muted} aria-live="off">
      <span className="caption__badge">
        {muted ? "Voice off — subtitles only" : "Live"}
      </span>
      <p className="caption__line" ref={lineRef}>
        {words.map((word, i) => (
          <span
            key={`${i}-${word}`}
            className="caption__word"
            // The most recent word is the one currently being voiced.
            data-current={i === last}
          >
            {word}
          </span>
        ))}
      </p>
    </div>
  );
}
