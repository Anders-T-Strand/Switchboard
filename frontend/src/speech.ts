// Read aloud: plays an answer spoken by ElevenLabs, through the server, one answer at a time.
//
// The audio streams with Media Source Extensions, so it starts before the clip is finished. Where MSE
// can't play MP3, the whole clip is fetched first and played from a blob.

export type SpeechEvent =
  | { kind: "loading" }
  /** firstAudioMs: measured on the server, from the request to ElevenLabs' first audio. heardMs: click to sound. */
  | { kind: "playing"; firstAudioMs: number | null; heardMs: number; cached: boolean; cut: boolean }
  | { kind: "ended" }
  | { kind: "error"; message: string };

type Listener = (id: number, event: SpeechEvent) => void;

const listeners = new Set<Listener>();
let current: { id: number; stop: () => void } | null = null;

export function onSpeech(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emit(id: number, event: SpeechEvent) {
  listeners.forEach((l) => l(id, event));
}

/** Stop whatever is playing. */
export function stopSpeech(): void {
  current?.stop();
}

export function canStreamMp3(): boolean {
  return typeof MediaSource !== "undefined" && MediaSource.isTypeSupported("audio/mpeg");
}

/** Read an answer aloud, or stop it when it is the one already playing. */
export async function speak(id: number): Promise<void> {
  if (current) {
    const playing = current.id;
    current.stop();
    if (playing === id) return; // pressing the button again stops it
  }
  const clicked = performance.now();
  const controller = new AbortController();
  const audio = new Audio();
  const urls: string[] = [];
  let over = false;

  const stop = () => {
    if (over) return;
    over = true;
    controller.abort(); // the server sees the request go away and records the play as stopped
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    urls.forEach((u) => URL.revokeObjectURL(u));
    if (current?.id === id) current = null;
    emit(id, { kind: "ended" });
  };
  current = { id, stop };
  emit(id, { kind: "loading" });

  try {
    const res = await fetch(`/api/requests/${id}/speech`, { signal: controller.signal });
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (typeof body?.detail === "string") message = body.detail;
      } catch {
        // keep the status line
      }
      throw new Error(message);
    }
    const header = res.headers.get("X-Speech-First-Audio-Ms");
    const firstAudioMs = header === null ? null : Number(header);
    const cached = res.headers.get("X-Speech-Cached") === "1";
    const cut = res.headers.get("X-Speech-Cut") === "1";
    audio.addEventListener(
      "playing",
      () => emit(id, { kind: "playing", firstAudioMs, heardMs: performance.now() - clicked, cached, cut }),
      { once: true },
    );
    audio.addEventListener("ended", stop, { once: true });

    if (canStreamMp3()) {
      await streamInto(audio, res, urls, () => over);
    } else {
      const blob = await res.blob();
      if (over) return;
      const url = URL.createObjectURL(blob);
      urls.push(url);
      audio.src = url;
      await audio.play();
    }
  } catch (e) {
    if (over) return; // stopped on purpose, which also aborts the fetch
    const message = e instanceof Error ? e.message : String(e);
    stop();
    emit(id, { kind: "error", message });
  }
}

async function streamInto(audio: HTMLAudioElement, res: Response, urls: string[], over: () => boolean) {
  if (!res.body) throw new Error("The audio didn't arrive.");
  const source = new MediaSource();
  const url = URL.createObjectURL(source);
  urls.push(url);
  audio.src = url;
  await new Promise<void>((resolve) => source.addEventListener("sourceopen", () => resolve(), { once: true }));
  const buffer = source.addSourceBuffer("audio/mpeg");
  const reader = res.body.getReader();
  let started = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (over()) {
      await reader.cancel();
      return;
    }
    if (done) break;
    await append(buffer, value);
    if (!started) {
      started = true;
      // Play as soon as there is something to play. play() waits for enough audio by itself.
      audio.play().catch((e: unknown) => {
        if (!over()) throw e;
      });
    }
  }
  if (source.readyState === "open") source.endOfStream();
}

function append(buffer: SourceBuffer, chunk: Uint8Array): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => {
      buffer.removeEventListener("error", failed);
      resolve();
    };
    const failed = () => {
      buffer.removeEventListener("updateend", done);
      reject(new Error("The audio couldn't be played."));
    };
    buffer.addEventListener("updateend", done, { once: true });
    buffer.addEventListener("error", failed, { once: true });
    buffer.appendBuffer(chunk as BufferSource);
  });
}
