/**
 * Subscribes to the backend's server messages and holds the deck state.
 *
 * This hook deliberately contains no logic about *which* slide should be
 * showing. The backend decides; this listens. The only thing computed here is
 * the toast copy, which is presentation, not state.
 */

import { RTVIEvent } from "@pipecat-ai/client-js";
import { useRTVIClientEvent } from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { ServerMessage, Slide, SlideChangeReason } from "../types";

export interface Toast {
  id: number;
  text: string;
  tone: "jump" | "refusal";
}

/** How long a toast stays on screen. */
const TOAST_MS = 3200;

function toastForSlideChange(
  reason: SlideChangeReason,
  title: string,
): string | null {
  switch (reason) {
    case "answer_question":
      // The interesting one: the agent decided to move, unprompted, to answer.
      return `Jumped to ${title}`;
    case "navigation":
      return `Moved to ${title}`;
    default:
      // Normal forward flow needs no explanation.
      return null;
  }
}

export function useSlides() {
  const [slides, setSlides] = useState<Slide[]>([]);
  const [current, setCurrent] = useState(1);
  const [total, setTotal] = useState(0);
  const [title, setTitle] = useState("");
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [refusals, setRefusals] = useState<string[]>([]);

  const nextToastId = useRef(0);
  const timers = useRef<number[]>([]);

  const pushToast = useCallback((text: string, tone: Toast["tone"]) => {
    const id = nextToastId.current++;
    setToasts((prev) => [...prev, { id, text, tone }]);
    const timer = window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, TOAST_MS);
    timers.current.push(timer);
  }, []);

  useEffect(
    () => () => {
      timers.current.forEach(window.clearTimeout);
    },
    [],
  );

  const onServerMessage = useCallback(
    (data: unknown) => {
      const msg = data as ServerMessage;
      if (!msg || typeof msg !== "object" || !("type" in msg)) return;

      switch (msg.type) {
        case "presentation-init":
          setSlides(msg.payload.slides);
          setCurrent(msg.payload.current);
          setTotal(msg.payload.total);
          setTitle(msg.payload.title);
          setHighlighted(null);
          break;

        case "slide-change": {
          setCurrent(msg.payload.slide);
          // A highlight only ever refers to the slide it was set on.
          setHighlighted(null);
          const text = toastForSlideChange(
            msg.payload.reason,
            msg.payload.title,
          );
          if (text) pushToast(text, "jump");
          break;
        }

        case "highlight":
          setHighlighted(msg.payload.bullet);
          break;

        case "refusal":
          setRefusals((prev) => [...prev, msg.payload.topic]);
          pushToast("Off-deck question declined", "refusal");
          break;
      }
    },
    [pushToast],
  );

  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);

  const reset = useCallback(() => {
    setCurrent(1);
    setHighlighted(null);
    setToasts([]);
    setRefusals([]);
  }, []);

  return {
    slides,
    current,
    total,
    title,
    highlighted,
    toasts,
    refusals,
    currentSlide: slides.find((s) => s.id === current) ?? null,
    reset,
  };
}
