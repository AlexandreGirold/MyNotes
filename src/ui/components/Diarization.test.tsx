import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import LiveNotes from "./LiveNotes";
import * as apiModule from "../api";

const request = vi.fn();
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(apiModule, "api").mockImplementation(request);
  request.mockReset();
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("downloads models through a queued job before allowing speaker capture", async () => {
  request.mockImplementation(async (path: string) => {
    if (path === "/settings") return { has_key: true };
    if (path === "/diarization") return { ready: false };
    if (path === "/diarization/setup") return { id: "setup" };
    if (path === "/jobs/setup") return { status: "succeeded", result: { ready: true } };
  });
  render(<LiveNotes active onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Pause audio" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: /distinguish speakers/i })).toBeDisabled();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /download speaker models/i })));
  expect(request).toHaveBeenCalledWith("/diarization/setup", { method: "POST" });
  expect(screen.getByRole("checkbox", { name: /distinguish speakers/i })).toBeEnabled();
});

it("keeps Voxtral raw text and labels speakers after Stop", async () => {
  let socket: any;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    bufferedAmount = 0;
    onopen: any; onmessage: any; onclose: any;
    constructor() { socket = this; queueMicrotask(() => this.onopen?.()); }
    send(raw: string) {
      const message = JSON.parse(raw);
      if (message.token) queueMicrotask(() => this.onmessage({ data: JSON.stringify({ type: "ready" }) }));
      if (message.type === "stop") this.onmessage({ data: JSON.stringify({ type: "done", text: "Bonjour. Non.", document_id: "raw", diarization_job_id: "speakers" }) });
    }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  class Context {
    sampleRate = 16000;
    audioWorklet = { addModule: async () => {} };
    destination = {};
    resume = async () => {}; suspend = async () => {}; close = async () => {};
    createMediaStreamSource = () => ({ connect() {} });
    createBiquadFilter = () => ({ type: "", frequency: { value: 0 }, connect() {} });
    createGain = () => ({ gain: { value: 0 }, connect() {} });
  }
  class Worklet {
    port = { onmessage: undefined as any, postMessage: () => this.port.onmessage({ data: { type: "flushed" } }) };
    connect() {} disconnect() {}
  }
  vi.stubGlobal("WebSocket", Socket); vi.stubGlobal("AudioContext", Context); vi.stubGlobal("AudioWorkletNode", Worklet);
  const track = { enabled: true, stop: vi.fn() };
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [track] }));
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  vi.spyOn(apiModule, "getConnection").mockResolvedValue({ url: "http://localhost:1", token: "test" });
  request.mockImplementation(async (path: string) => {
    if (path === "/settings") return { has_key: true, realtime_model: "mistralai/Voxtral-Mini-4B-Realtime-2602" };
    if (path === "/provider/models") return ["mistralai/Voxtral-Mini-4B-Realtime-2602"];
    if (path === "/diarization") return { ready: true };
    if (path === "/jobs/speakers") return { status: "succeeded", result: { document_id: "labelled", text: "Speaker 1: Bonjour.\nSpeaker 2: Non.", warnings: [] } };
    if (path === "/chat") return { id: "summary" };
    if (path === "/jobs/summary") return { status: "succeeded", result: { answer: "Deux avis différents." } };
  });
  render(<LiveNotes active onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("checkbox", { name: /distinguish speakers/i })).toBeEnabled());
  fireEvent.click(screen.getByRole("checkbox", { name: /distinguish speakers/i }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /start live transcription/i })));
  await waitFor(() => expect(screen.getByRole("button", { name: /stop recording/i })).toBeEnabled());
  act(() => socket.onmessage({ data: JSON.stringify({ type: "delta", delta: "Bonjour. Non." }) }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Pause audio" })));
  expect(track.enabled).toBe(false);
  expect(request.mock.calls.some(c => c[0] === "/chat")).toBe(false);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume audio" })));
  expect(track.enabled).toBe(true);
  expect(getUserMedia).toHaveBeenCalledWith({ audio: expect.objectContaining({ autoGainControl: false }) });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /stop recording/i })));
  expect(request.mock.calls.some(c => c[0] === "/chat")).toBe(false);
  await waitFor(() => expect(screen.getByLabelText("Live transcript")).toHaveValue("Speaker 1: Bonjour.\nSpeaker 2: Non."));
  fireEvent.click(screen.getByRole("button", { name: "Original transcript" }));
  expect(screen.getByLabelText("Live transcript")).toHaveValue("Bonjour. Non.");
});
