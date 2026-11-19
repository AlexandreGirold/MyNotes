import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import LiveNotes from "./LiveNotes";
import * as apiMod from "../api";

const mockApi = vi.fn();
const mockGetConnection = vi.fn();
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(apiMod, "api").mockImplementation(mockApi);
  vi.spyOn(apiMod, "getConnection").mockImplementation(mockGetConnection);
  mockApi.mockResolvedValue({ has_key: false });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("opens settings without a key and permits editing the transcript", async () => {
  const onSettings = vi.fn();
  render(<LiveNotes active onJob={vi.fn()} onSaved={vi.fn()} onSettings={onSettings} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /connect cloud temple/i })));
  expect(onSettings).toHaveBeenCalledOnce();
  expect(mockGetConnection).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Live transcript"), { target: { value: "Hello world" } });
  expect(screen.getByLabelText("Live transcript")).toHaveValue("Hello world");
  expect(screen.getByRole("button", { name: "Save transcript as…" })).not.toBeDisabled();
});

it("mixes the voice filter at 50% without native noise suppression or automatic gain", async () => {
  mockApi.mockImplementation(async path => path === "/provider/models" ? ["voxtral"] : { has_key: true, realtime_model: "voxtral" });
  mockGetConnection.mockResolvedValue({ url: "http://127.0.0.1:8000", token: "test" });
  let socket: any, processor: any;
  const send = vi.fn(), stop = vi.fn();
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    readyState = 1; bufferedAmount = 0;
    onopen: any; onmessage: any;
    send = send; close = vi.fn();
    constructor() { socket = this; }
  });
  const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] });
  const filters: any[] = [], gains: any[] = [];
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
  vi.stubGlobal("AudioContext", class {
    sampleRate = 16000;
    audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
    resume = vi.fn().mockResolvedValue(undefined);
    close = vi.fn().mockResolvedValue(undefined);
    destination = {};
    createMediaStreamSource() { return { connect: vi.fn() }; }
    createBiquadFilter() { const node = { type: "", frequency: { value: 0 }, connect: vi.fn() }; filters.push(node); return node; }
    createGain() { const node = { gain: { value: 1 }, connect: vi.fn() }; gains.push(node); return node; }
  });
  vi.stubGlobal("AudioWorkletNode", class {
    port = { onmessage: null }; connect = vi.fn(); disconnect = vi.fn();
    constructor() { processor = this; }
  });
  const { unmount } = render(<LiveNotes active onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
  const button = await screen.findByRole("button", { name: /Start live transcription/ });
  fireEvent.change(screen.getByLabelText("Live transcript"), { target: { value: "Earlier notes." } });
  expect(button).toHaveTextContent("Restart live transcription");
  await act(async () => { fireEvent.click(button); });
  expect(screen.getByLabelText("Live transcript")).toHaveValue("Earlier notes.");
  await act(async () => { socket.onopen(); socket.onmessage({ data: JSON.stringify({ type: "ready" }) }); });
  expect(JSON.parse(send.mock.calls[0][0]).initial_text).toBe("Earlier notes.");
  expect(getUserMedia).toHaveBeenCalledWith({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
  expect(filters.map(node => [node.type, node.frequency.value])).toEqual([["highpass", 80], ["lowpass", 6000]]);
  expect(gains.map(node => node.gain.value)).toEqual([0.5, 0.5, 0]);
  expect(gains[0].connect).toHaveBeenCalledWith(processor);
  expect(gains[1].connect).toHaveBeenCalledWith(processor);
  expect(screen.getByText("Listening")).toBeInTheDocument();
  const buffer = new ArrayBuffer(5120);
  act(() => processor.port.onmessage({ data: { type: "audio", buffer, level: 0.1 } }));
  expect(send).toHaveBeenLastCalledWith(buffer);
  expect(screen.getByLabelText("Microphone signal")).toHaveAttribute("value", "0.5");
  expect(screen.queryByLabelText("Microphone boost")).not.toBeInTheDocument();
  await act(async () => { socket.onmessage({ data: JSON.stringify({ type: "delta", delta: "\n\nNew speech." }) }); });
  await act(async () => { socket.onmessage({ data: JSON.stringify({ type: "done", text: "Earlier notes.\n\nNew speech." }) }); });
  expect(screen.getByLabelText("Live transcript")).toHaveValue("Earlier notes.\n\nNew speech.");
  unmount(); expect(stop).toHaveBeenCalled();
});
