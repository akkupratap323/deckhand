/**
 * Client setup and routing.
 *
 * Hash routes, so the built app can be served as a single static file with no
 * server-side rewrite rules:
 *
 *   #/              home: upload a deck, see the library
 *   #/deck/:id      watch it parse, then review it before starting
 *   #/present/:id   present that deck
 *
 * The Pipecat client is created once, outside the component, so a re-render
 * never tears down a live WebRTC session.
 */

import { PipecatClient } from "@pipecat-ai/client-js";
import {
  PipecatClientAudio,
  PipecatClientProvider,
} from "@pipecat-ai/client-react";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { useEffect, useState } from "react";

import { DeckPage } from "./pages/Deck";
import { Home } from "./pages/Home";
import { Present } from "./pages/Present";

const client = new PipecatClient({
  transport: new SmallWebRTCTransport(),
  enableMic: true,
  enableCam: false,
});

type Route =
  | { name: "home" }
  | { name: "deck"; deckId: string }
  | { name: "present"; deckId: string };

function parseHash(): Route {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "present" && parts[1]) {
    return { name: "present", deckId: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "deck" && parts[1]) {
    return { name: "deck", deckId: decodeURIComponent(parts[1]) };
  }
  return { name: "home" };
}

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return route;
}

export default function App() {
  const route = useRoute();

  return (
    <PipecatClientProvider client={client}>
      {route.name === "present" ? (
        // Keyed by deck so switching decks remounts rather than reusing state.
        <Present key={route.deckId} deckId={route.deckId} />
      ) : route.name === "deck" ? (
        <DeckPage key={route.deckId} deckId={route.deckId} />
      ) : (
        <Home />
      )}
      {/* Renders the agent's audio track. Without this there is no sound. */}
      <PipecatClientAudio />
    </PipecatClientProvider>
  );
}
