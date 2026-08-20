/**
 * The presenter. Connects a voice session bound to one deck.
 *
 * The deck id travels to the backend as a query parameter on the signalling
 * endpoint rather than in the body: SmallWebRTCTransport nests anything passed
 * as `requestData` under a `requestData` key instead of merging it, and a query
 * parameter avoids depending on that shape.
 */

import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  useRTVIClientEvent,
  usePipecatClient,
  usePipecatClientMediaTrack,
  usePipecatClientTransportState,
  usePipecatConversation,
} from "@pipecat-ai/client-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { SERVER_URL, getDeck, getInsights, listPresenters } from "../api";
import { Controls } from "../components/Controls";
import { Insights } from "../components/Insights";
import { LiveCaption } from "../components/LiveCaption";
import { PresenterPicker } from "../components/PresenterPicker";
import { SlideView } from "../components/SlideView";
import { Thumbnails } from "../components/Thumbnails";
import { partText } from "../components/TranscriptList";
import { useSlides } from "../hooks/useSlides";
import { playStartChime } from "../lib/chime";
import type {
  AgentActivity,
  DeckDetail,
  Presenter,
  SessionInsights,
  TranscriptTurn,
} from "../types";

interface Props {
  deckId: string;
}

export function Present({ deckId }: Props) {
  const pipecatClient = usePipecatClient();
  const transportState = usePipecatClientTransportState();
  const { slides, current, total, highlighted, toasts, currentSlide, title, reset } =
    useSlides();

  const [activity, setActivity] = useState<AgentActivity>("idle");
  const [error, setError] = useState<string | null>(null);
  // True from the moment Start is pressed until connect resolves or fails.
  const [busy, setBusy] = useState(false);
  // The deck as fetched over REST. Rendered before the voice session starts, so
  // the page shows the actual slides instead of an empty stage while idle.
  const [preview, setPreview] = useState<DeckDetail | null>(null);

  // Who presents. Chosen before connecting and fixed for the session -
  // the voice is baked into the pipeline when it is built, so this cannot
  // be changed live without tearing the session down.
  const [presenters, setPresenters] = useState<Presenter[]>([]);
  const [presenterId, setPresenterId] = useState<string | null>(null);
  // Set once a session has actually started, so ending one returns to the
  // picker rather than stranding the page on an idle stage.
  const [started, setStarted] = useState(false);

  // The agent's voice, muted at the track rather than the element: it silences
  // playback without touching the pipeline, so navigation and the transcript
  // carry on while the agent is quiet.
  const botTrack = usePipecatClientMediaTrack("audio", "bot");
  const [botAudioOn, setBotAudioOn] = useState(true);

  const { messages } = usePipecatConversation();
  const [insights, setInsights] = useState<SessionInsights | null>(null);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsError, setInsightsError] = useState<string | null>(null);

  const connected = transportState === "ready";
  const connecting =
    transportState === "connecting" ||
    transportState === "initializing" ||
    transportState === "authenticating";

  // The presenter list is static server-side; fetch it once alongside the
  // deck so the picker is ready the moment the page is.
  useEffect(() => {
    let cancelled = false;
    listPresenters()
      .then(({ presenters: list, default: fallback }) => {
        if (cancelled) return;
        setPresenters(list);
        setPresenterId((current) => current ?? fallback ?? list[0]?.id ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // Load the deck up front so the page has a title and slide count to show
  // before the voice session hands over the real thing.
  useEffect(() => {
    let cancelled = false;
    getDeck(deckId)
      .then((deck) => {
        if (!cancelled) setPreview(deck);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not load that deck.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [deckId]);

  // Apply the mute whenever the toggle changes or a new track arrives - a
  // reconnect hands over a fresh track that defaults to enabled.
  //
  // The linter flags this as mutating a hook's return value. It is, and it is
  // correct here: a MediaStreamTrack is a live browser object rather than
  // React state, and `.enabled` is the WebRTC API for muting one. Silencing
  // the track leaves the pipeline untouched, which is what keeps slide
  // navigation and the transcript running while the agent is quiet.
  useEffect(() => {
    if (botTrack) botTrack.enabled = botAudioOn;
  }, [botTrack, botAudioOn]);

  const toggleBotAudio = useCallback(() => setBotAudioOn((on) => !on), []);

  // The transcript, flattened for analysis. Empty assistant turns exist the
  // moment a reply starts streaming, so they are dropped here rather than
  // sent and filtered server-side.
  const turns = useMemo<TranscriptTurn[]>(
    () =>
      messages
        .filter((m) => m.role === "user" || m.role === "assistant")
        .map((m) => ({
          role: m.role as "user" | "assistant",
          text: m.parts
            .map((part) => {
              const { spoken, unspoken } = partText(part);
              return `${spoken}${unspoken}`;
            })
            .join("")
            .trim(),
        }))
        .filter((t) => t.text.length > 0),
    [messages],
  );

  const askedSomething = turns.some((t) => t.role === "user");

  const generateInsights = useCallback(async () => {
    setInsightsLoading(true);
    setInsightsError(null);
    try {
      setInsights(await getInsights(deckId, turns));
    } catch (err) {
      setInsights(null);
      setInsightsError(
        err instanceof Error ? err.message : "Could not analyse this session.",
      );
    } finally {
      setInsightsLoading(false);
    }
  }, [deckId, turns]);

  // --- activity indicator ---
  // User speech wins over bot speech: if both fire, the user interrupted, and
  // showing "Listening" immediately is the visible half of barge-in working.
  useRTVIClientEvent(
    RTVIEvent.UserStartedSpeaking,
    useCallback(() => setActivity("listening"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.BotLlmStarted,
    useCallback(() => setActivity("thinking"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.BotStartedSpeaking,
    useCallback(() => setActivity("speaking"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.BotStoppedSpeaking,
    useCallback(() => setActivity("idle"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.Disconnected,
    useCallback(() => setActivity("idle"), []),
  );

  const handleConnect = useCallback(async () => {
    if (!pipecatClient) return;
    // Guard re-entry. Without this a second click builds a second peer
    // connection while the first is still negotiating, and the agent ends up
    // talking down a connection the browser has already replaced.
    if (busy || connected || connecting) return;

    setBusy(true);
    setError(null);
    reset();
    // A previous session's report has nothing to do with this one.
    setInsights(null);
    setInsightsError(null);
    setBotAudioOn(true);
    setStarted(true);

    try {
      // Ask for the microphone explicitly, before negotiating. The transport
      // will happily connect without it and then sit there receiving no audio,
      // which looks like a broken agent rather than a denied permission.
      await pipecatClient.initDevices();

      const mics = await pipecatClient.getAllMics();
      const usable = mics.filter((m) => m.deviceId);
      if (usable.length === 0) {
        throw new Error(
          "No microphone is available. Allow microphone access for this site, " +
            "then reload and try again.",
        );
      }

      await pipecatClient.connect({
        webrtcRequestParams: {
          endpoint:
            `${SERVER_URL}/api/offer?deck_id=${encodeURIComponent(deckId)}` +
            (presenterId ? `&presenter=${encodeURIComponent(presenterId)}` : ""),
        },
      });

      // Marks the moment the session is actually live. Fired after connect
      // resolves, not on the click, so it never promises a session that then
      // fails to start. Runs inside the click's gesture context, which is what
      // keeps autoplay policy from blocking it.
      void playStartChime();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // getUserMedia rejections are the common case and their default text is
      // not actionable, so name the fix.
      const denied =
        /permission|denied|NotAllowed|NotFound|microphone/i.test(message);
      setError(
        denied
          ? `Microphone problem: ${message}. Click the padlock in the address bar, ` +
              "allow the microphone for this site, then reload."
          : `Could not start the session: ${message}`,
      );
      setActivity("idle");
    } finally {
      setBusy(false);
    }
  }, [pipecatClient, reset, deckId, busy, connected, connecting, presenterId]);

  const handleDisconnect = useCallback(async () => {
    await pipecatClient?.disconnect();
    setActivity("idle");
  }, [pipecatClient]);

  // Leaving the page mid-session would otherwise leave the bot running.
  useEffect(
    () => () => {
      void pipecatClient?.disconnect();
    },
    [pipecatClient],
  );

  // Once the session is live its deck wins; before that, fall back to the one
  // fetched over REST so the page is never blank.
  const heading = title || preview?.title || "Presentation";
  const deckSlides = slides.length > 0 ? slides : (preview?.slides ?? []);
  const slideTotal = total || preview?.slide_count || 0;
  const shownSlide = deckSlides.find((s) => s.id === current) ?? deckSlides[0] ?? null;

  // The picker owns the page until a session has been started. Showing the
  // stage and the picker together would offer two competing "begin" actions.
  const choosing = !started && !connected && !connecting;
  const chosen = presenters.find((p) => p.id === presenterId) ?? null;

  return (
    <div className="app">
      <header className="header">
        <div>
          <a className="backlink" href="#/">
            ← All decks
          </a>
          <h1 className="header__title">{heading}</h1>
          <p className="header__sub">
            {choosing
              ? "Pick a presenter, then start. Their voice and delivery are fixed for the session."
              : "Interrupt any time and ask a question — the agent will jump to the slide that answers it."}
          </p>
        </div>
        {!choosing && (
          <Controls
            activity={activity}
            connected={connected}
            connecting={connecting || busy}
            current={current}
            total={slideTotal}
            onConnect={handleConnect}
            onDisconnect={handleDisconnect}
            botAudioOn={botAudioOn}
            onToggleBotAudio={toggleBotAudio}
          />
        )}
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {choosing && presenters.length > 0 ? (
        <PresenterPicker
          presenters={presenters}
          selected={presenterId}
          onSelect={setPresenterId}
          onStart={() => void handleConnect()}
          busy={busy || connecting}
          deckTitle={heading}
        />
      ) : (
      <main className="main">
        <div className="stage">
          {chosen && (
            <p className="stage__presenter">
              Presented by <strong>{chosen.name}</strong> · {chosen.tagline}
            </p>
          )}
          <SlideView
            slide={currentSlide ?? shownSlide}
            highlighted={highlighted}
            connected={connected}
            deckId={deckId}
            hasImages={preview?.has_images ?? false}
          />
          {/* Subtitles, word by word, in step with the voice. Directly under
              the slide because that is where the eye already is. */}
          <LiveCaption active={connected} muted={!botAudioOn} />
          <Thumbnails slides={deckSlides} current={current} />
        </div>
      </main>
      )}

      {/* Mounted whenever there is a conversation to show. The Analysis tab
          gates itself on the session being over; the Transcript tab is useful
          the moment anyone speaks. */}
      {turns.length > 0 && (
        <Insights
          insights={insights}
          loading={insightsLoading}
          error={insightsError}
          canGenerate={askedSomething && !connected && !connecting}
          onGenerate={() => void generateInsights()}
          messages={messages}
        />
      )}

      <div className="toasts" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast--${toast.tone}`}>
            {toast.text}
          </div>
        ))}
      </div>
    </div>
  );
}
