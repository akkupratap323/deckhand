/**
 * The post-session panel: analysis and the full transcript.
 *
 * This is where the conversation lives once it is over. During the call the
 * subtitles carry the spoken word and the slide gets the whole stage; reading
 * back what was said is something you do afterwards, so the transcript moved
 * here rather than competing with the slide for space.
 *
 * Two tabs, because the two halves have different costs and different
 * lifetimes. The transcript exists the moment anyone speaks and is free to
 * show. The analysis costs a model call and is generated on request, so it has
 * idle, loading and error states of its own - none of which should block
 * getting at the transcript.
 *
 * The distinction the backend draws between *counted* and *judged* numbers is
 * carried through into this layout deliberately. Counts (questions asked, words
 * spoken) are exact and presented plainly. Scores are a model's opinion and are
 * always shown next to the reasoning behind them, so a reader can disagree with
 * a 3 instead of taking it as a measurement.
 */

import type { ConversationMessage } from "@pipecat-ai/client-react";
import { useState } from "react";

import type { SessionInsights } from "../types";
import { TranscriptList } from "./TranscriptList";

interface Props {
  insights: SessionInsights | null;
  loading: boolean;
  error: string | null;
  /** False when nobody asked anything, so there is nothing to evaluate. */
  canGenerate: boolean;
  onGenerate: () => void;
  messages: ConversationMessage[];
}

const INTENT_LABELS: Record<string, string> = {
  factual_lookup: "Factual lookup",
  clarification: "Clarification",
  deeper_detail: "Deeper detail",
  challenge: "Challenge",
  off_topic: "Off deck",
  navigation: "Navigation",
  social: "Social",
  unclassified: "Unclassified",
};

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat">
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
    </div>
  );
}

function List({ title, items, tone }: { title: string; items: string[]; tone?: string }) {
  if (items.length === 0) return null;
  return (
    <div className="insight-list" data-tone={tone}>
      <h4 className="insight-list__title">{title}</h4>
      <ul>
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function Analysis({ insights }: { insights: SessionInsights }) {
  const { counts, scores } = insights;
  const answeredPct =
    scores.evaluated > 0 ? Math.round((scores.answered / scores.evaluated) * 100) : null;

  return (
    <>
      {insights.summary && <p className="insights__summary">{insights.summary}</p>}

      <div className="stats">
        <Stat label="Questions asked" value={counts.questions_asked} />
        <Stat
          label="Answer quality"
          value={scores.average_quality === null ? "—" : `${scores.average_quality} / 5`}
        />
        <Stat label="Answered" value={answeredPct === null ? "—" : `${answeredPct}%`} />
        <Stat label="Grounded in deck" value={`${scores.grounded}/${scores.evaluated}`} />
      </div>

      {insights.audience_intent && (
        <p className="insights__intent">
          <strong>What the audience wanted:</strong> {insights.audience_intent}
        </p>
      )}

      {insights.exchanges.length > 0 && (
        <div className="exchanges">
          <h4 className="insight-list__title">Question by question</h4>
          {insights.exchanges.map((ex, i) => (
            <article key={i} className="exchange">
              <header className="exchange__head">
                <span className="exchange__q">{ex.question || `Question ${i + 1}`}</span>
                <span className="exchange__tags">
                  <span className="tag" data-intent={ex.intent}>
                    {INTENT_LABELS[ex.intent] ?? ex.intent}
                  </span>
                  <span className="tag tag--score" data-score={ex.quality}>
                    {ex.quality > 0 ? `${ex.quality}/5` : "—"}
                  </span>
                </span>
              </header>
              {ex.intent_note && <p className="exchange__note">{ex.intent_note}</p>}
              {ex.quality_note && (
                // The reasoning sits next to the score on purpose - see the
                // module docstring on judged vs counted numbers.
                <p className="exchange__why">{ex.quality_note}</p>
              )}
              <div className="exchange__flags">
                <span className="flag" data-on={ex.answered}>
                  {ex.answered ? "Answered" : "Not answered"}
                </span>
                <span className="flag" data-on={ex.grounded}>
                  {ex.grounded ? "Grounded" : "Ungrounded"}
                </span>
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="insights__cols">
        <List title="What went well" items={insights.strengths} tone="good" />
        <List title="What did not" items={insights.weaknesses} tone="warn" />
        <List title="Gaps in the deck" items={insights.deck_gaps} tone="warn" />
        <List title="Recommendations" items={insights.recommendations} tone="accent" />
      </div>

      <p className="insights__foot">
        {counts.total_turns} turns · {counts.user_words} words from the audience ·{" "}
        {counts.agent_words} from the agent
      </p>
    </>
  );
}

export function Insights({
  insights,
  loading,
  error,
  canGenerate,
  onGenerate,
  messages,
}: Props) {
  const [tab, setTab] = useState<"analysis" | "transcript">("analysis");

  const turnCount = messages.filter(
    (m) => m.role === "user" || m.role === "assistant",
  ).length;

  return (
    <section className="insights">
      <div className="insights__top">
        <div className="tabs" role="tablist" aria-label="Session detail">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "analysis"}
            className="tabs__tab"
            data-active={tab === "analysis"}
            onClick={() => setTab("analysis")}
          >
            Analysis
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "transcript"}
            className="tabs__tab"
            data-active={tab === "transcript"}
            onClick={() => setTab("transcript")}
          >
            Transcript
            {turnCount > 0 && <span className="tabs__count">{turnCount}</span>}
          </button>
        </div>

        {tab === "analysis" && insights && !loading && (
          <button type="button" className="btn btn--quiet btn--sm" onClick={onGenerate}>
            Re-analyse
          </button>
        )}
      </div>

      {tab === "transcript" ? (
        <div className="insights__transcript">
          <TranscriptList
            messages={messages}
            emptyText="Nothing was said in this session."
          />
        </div>
      ) : loading ? (
        <div aria-busy="true">
          <p className="insights__blurb">Analysing the conversation…</p>
          <div className="insights__bar">
            <span />
          </div>
        </div>
      ) : error ? (
        <div className="insights__state insights__state--bad">
          <p className="insights__blurb">{error}</p>
          <button type="button" className="btn" onClick={onGenerate}>
            Try again
          </button>
        </div>
      ) : insights ? (
        <Analysis insights={insights} />
      ) : (
        <div className="insights__state">
          <p className="insights__blurb">
            {canGenerate
              ? "Analyse this conversation: what the audience was asking, how well the agent answered, and what the deck was missing."
              : "Ask the agent something during a session, then come back here for an analysis of how it went."}
          </p>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onGenerate}
            disabled={!canGenerate}
          >
            Generate insights
          </button>
        </div>
      )}
    </section>
  );
}
