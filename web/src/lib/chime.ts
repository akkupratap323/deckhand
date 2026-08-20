/**
 * The short crystal tone that marks a session opening.
 *
 * Synthesised with the Web Audio API rather than shipped as a file: it is a
 * few sine partials and an exponential decay, which costs nothing to generate
 * and keeps the bundle free of a binary asset that would need licensing.
 *
 * The sound is three partials of one bell-like tone - a root, its octave, and
 * a twelfth - each decaying at a slightly different rate, which is what makes
 * a struck-glass sound rather than a beep. Frequencies are from the E-major
 * triad an octave up, high enough to read as "ready" without being shrill.
 */

/** Partial frequencies (Hz), gains, and decay times (s). */
const PARTIALS: { freq: number; gain: number; decay: number }[] = [
  { freq: 1318.5, gain: 0.28, decay: 1.5 }, // E6 - the fundamental
  { freq: 1975.5, gain: 0.14, decay: 1.1 }, // B6 - the twelfth
  { freq: 2637.0, gain: 0.08, decay: 0.85 }, // E7 - the octave, adds the glint
];

/**
 * Play the chime. Never throws.
 *
 * Audio is decoration here, and every failure mode is environmental - a
 * browser that blocks audio until a gesture, a missing AudioContext, a device
 * with no output. None of those are worth surfacing to someone who just wanted
 * to start a presentation, so they are swallowed.
 */
export async function playStartChime(volume = 0.5): Promise<void> {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctor) return;

    const ctx = new Ctor();
    // Autoplay policy suspends contexts created outside a gesture. This runs
    // from a click, so resuming is normally instant.
    if (ctx.state === "suspended") await ctx.resume();

    const now = ctx.currentTime;
    const master = ctx.createGain();
    master.gain.value = Math.max(0, Math.min(1, volume));
    master.connect(ctx.destination);

    let longest = 0;
    for (const { freq, gain, decay } of PARTIALS) {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();

      osc.type = "sine";
      osc.frequency.value = freq;

      // A 6ms attack instead of an instant one: a hard start produces an
      // audible click as the waveform jumps from silence.
      env.gain.setValueAtTime(0, now);
      env.gain.linearRampToValueAtTime(gain, now + 0.006);
      env.gain.exponentialRampToValueAtTime(0.0001, now + decay);

      osc.connect(env);
      env.connect(master);
      osc.start(now);
      osc.stop(now + decay);
      longest = Math.max(longest, decay);
    }

    // Free the hardware once the tail has rung out. Leaked contexts are a
    // real limit - browsers cap how many a page may hold open.
    window.setTimeout(() => void ctx.close().catch(() => undefined), (longest + 0.1) * 1000);
  } catch {
    /* decoration only; never let a chime break a session */
  }
}
