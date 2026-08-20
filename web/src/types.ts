/**
 * Shapes of the messages the backend pushes over the RTVI server-message
 * channel. The backend is the authority on all of this; the frontend only
 * renders what it is told.
 */

export interface Slide {
  id: number;
  title: string;
  bullets: string[];
}

/** Why the agent moved. Drives the toast copy, so the reason is visible. */
export type SlideChangeReason = "answer_question" | "navigation" | "presenting";

export interface PresentationInit {
  type: "presentation-init";
  payload: {
    deck_id: string;
    title: string;
    slides: Slide[];
    current: number;
    total: number;
  };
}

export interface SlideChange {
  type: "slide-change";
  payload: {
    slide: number;
    reason: SlideChangeReason;
    title: string;
    total: number;
  };
}

export interface Highlight {
  type: "highlight";
  payload: { slide: number; bullet: number; text: string };
}

export interface Refusal {
  type: "refusal";
  payload: { topic: string; slide: number };
}

export type ServerMessage =
  | PresentationInit
  | SlideChange
  | Highlight
  | Refusal;

/** What the agent is doing right now, for the status indicator. */
export type AgentActivity = "idle" | "listening" | "thinking" | "speaking";

/** Where a deck is in its ingestion lifecycle. */
export type IngestStatus = "processing" | "ready" | "failed";

/**
 * Which stage of ingestion is running, in the order they occur.
 *
 * Rendering and narration have very different durations, so the parsing view
 * shows them as distinct steps rather than averaging both into one bar that
 * would move at a misleading rate. "converting" only occurs for PowerPoint.
 */
export type IngestPhase =
  | "queued"
  | "converting"
  | "rendering"
  | "narrating"
  | "synthesizing"
  | "done";

/** Library-listing shape: everything except slide content. */
export interface DeckSummary {
  id: string;
  title: string;
  status: IngestStatus;
  error: string | null;
  source: string | null;
  created_at: string;
  slide_count: number;
  /** Slides narrated so far, for the progress bar. */
  progress: number;
  /** The deck that ships with the app; cannot be deleted. */
  builtin: boolean;
  /** Whether rendered page images exist. False for the built-in deck. */
  has_images: boolean;
  /** Whether the deck-level intro/transitions/outro pass has run. */
  has_synthesis: boolean;
  /** Which ingestion stage is running. "done" once nothing is in flight. */
  phase: IngestPhase;
  /** Pages the source has. Known before any of them finish rendering. */
  page_count: number;
  /** Page images written so far; each is immediately fetchable. */
  pages_rendered: number;
}

export interface DeckDetail extends DeckSummary {
  slides: Slide[];
}

/** The polling payload while a deck ingests. */
export interface DeckStatus {
  id: string;
  status: IngestStatus;
  error: string | null;
  progress: number;
  slide_count: number;
  title: string;
  phase: IngestPhase;
  page_count: number;
  pages_rendered: number;
  has_images: boolean;
}

/** Server capabilities, so the UI can say up front what will be accepted. */
export interface Health {
  status: string;
  active_sessions: number;
  decks: number;
  vision_narration: boolean;
  /** False when LibreOffice is absent, which makes .pptx upload fail. */
  pptx_supported: boolean;
  configured: Record<string, boolean>;
}

/** One evaluated audience question from the post-session analysis. */
export interface InsightExchange {
  question: string;
  /** Backend-validated; unrecognised values arrive as "unclassified". */
  intent: string;
  intent_note: string;
  answered: boolean;
  grounded: boolean;
  /** 1-5, or 0 when the model did not produce a usable score. */
  quality: number;
  quality_note: string;
}

/**
 * The post-session report.
 *
 * `counts` are computed exactly, server-side, from the transcript. `scores`
 * are derived from the per-exchange judgements so the headline always agrees
 * with the rows a reader can see. Only the prose and the per-exchange verdicts
 * are the model's opinion.
 */
export interface SessionInsights {
  deck_title: string;
  summary: string;
  audience_intent: string;
  exchanges: InsightExchange[];
  strengths: string[];
  weaknesses: string[];
  deck_gaps: string[];
  recommendations: string[];
  scores: {
    average_quality: number | null;
    answered: number;
    grounded: number;
    evaluated: number;
  };
  counts: {
    questions_asked: number;
    agent_replies: number;
    user_words: number;
    agent_words: number;
    total_turns: number;
  };
}

/** One spoken turn, as submitted for analysis. */
export interface TranscriptTurn {
  role: "user" | "assistant";
  text: string;
}

/**
 * A selectable presenter: a voice plus a manner of speaking.
 *
 * The delivery style itself is not sent to the browser - it is prompt
 * engineering, not user-facing copy. What arrives is what the card needs.
 */
export interface Presenter {
  id: string;
  name: string;
  /** Short role label, e.g. "The Analyst". */
  tagline: string;
  /** One sentence on when this presenter suits a deck. */
  blurb: string;
  accent: string;
  tone: string[];
  /** One-word grouping shown as a pill on the card. */
  category: string;
  /** Glyph name; the picker maps it to an inline SVG path. */
  icon: string;
  /** Two hex stops for the card's orb gradient. */
  colors: [string, string];
}
