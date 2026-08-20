/**
 * The conversation, rendered as a list of turns.
 *
 * Split out of the old right-hand transcript panel so the same markup can be
 * used after the session, inside the insights report, where a transcript is
 * actually useful. During the call the subtitles carry the spoken word and the
 * slide gets the whole stage; re-reading what was said is a post-call activity,
 * not something to do while the agent is mid-sentence.
 *
 * A message part's `text` is not always a plain string. For assistant turns the
 * client library gives back `BotOutputText`, `{ spoken, unspoken }` - `spoken`
 * is the portion already vocalized by TTS, `unspoken` is LLM output generated
 * but not yet said. Treating that object as a string renders "[object Object]".
 */

import type { BotOutputText, ConversationMessagePart } from "@pipecat-ai/client-react";
import type { ConversationMessage } from "@pipecat-ai/client-react";

export function partText(part: ConversationMessagePart): {
  spoken: string;
  unspoken: string;
} {
  const { text } = part;
  if (text && typeof text === "object" && "spoken" in text) {
    const bot = text as BotOutputText;
    return { spoken: bot.spoken ?? "", unspoken: bot.unspoken ?? "" };
  }
  // User turns (and any custom injected content) are plain strings.
  return { spoken: typeof text === "string" ? text : "", unspoken: "" };
}

interface Props {
  messages: ConversationMessage[];
  /** Shown when there is nothing to display. */
  emptyText?: string;
}

export function TranscriptList({ messages, emptyText }: Props) {
  const spokenTurns = messages.filter(
    (m) => m.role === "user" || m.role === "assistant",
  );

  const rendered = spokenTurns
    .map((message) => {
      const said = message.parts.map((p) => partText(p).spoken).join("");
      const pending = message.parts.map((p) => partText(p).unspoken).join("");
      if (!said.trim() && !pending.trim()) return null;

      const isUser = message.role === "user";
      return (
        <div key={message.createdAt} className={`turn turn--${message.role}`}>
          <span className="turn__avatar" aria-hidden="true">
            {isUser ? "Y" : "A"}
          </span>
          <div className="turn__bubble">
            <span className="turn__who">{isUser ? "You" : "Agent"}</span>
            <p className="turn__text">
              {said}
              {/* Generated but not yet voiced - a dim preview that resolves as
                  speech catches up to it. */}
              {pending && <span className="turn__pending">{pending}</span>}
            </p>
          </div>
        </div>
      );
    })
    .filter(Boolean);

  if (rendered.length === 0) {
    return (
      <p className="transcript__empty">
        {emptyText ?? "Nothing was said in this session."}
      </p>
    );
  }

  return <div className="turns">{rendered}</div>;
}
