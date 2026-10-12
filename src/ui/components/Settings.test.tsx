import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Settings from "./Settings";
import * as apiModule from "../api";

afterEach(() => { vi.restoreAllMocks(); delete (window as any).mynotes; });

it("shows published model speeds, account ranking and preserves a custom selection", async () => {
  vi.spyOn(apiModule, "api").mockImplementation(async (path: string) => path === "/provider/models" ? ["gpt-oss:120b", "qwen3.6:27b", "voxtral"] : { has_key: true, global_chat_model: "custom-model", realtime_model: "mistralai/Voxtral-Mini-4B-Realtime-2602" });
  (window as any).mynotes = { getMicrophoneVolume: vi.fn().mockResolvedValue({ volume: 27, muted: false }) };
  render(<Settings onJob={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "General conversations" })).toHaveValue("custom-model"));
  await waitFor(() => expect(screen.getAllByRole("option", { name: /gpt-oss:120b · 94 tokens\/s · speed #1\/2 · available/ }).length).toBeGreaterThan(0));
  fireEvent.change(screen.getByRole("combobox", { name: "General conversations" }), { target: { value: "qwen3.6:35b" } });
  expect(screen.getByRole("textbox", { name: "General conversations custom model ID" })).toHaveValue("qwen3.6:35b");
  expect(screen.getByText(/Full Cloud Temple catalogue · 32 models/)).toBeInTheDocument();
});

it("reads actual system volume and applies a chosen level on release or keyboard", async () => {
  vi.spyOn(apiModule, "api").mockResolvedValue({ has_key: false });
  const setVolume = vi.fn().mockImplementation(async volume => ({ volume, muted: false }));
  (window as any).mynotes = { getMicrophoneVolume: vi.fn().mockResolvedValue({ volume: 27, muted: false }), setMicrophoneVolume: setVolume };
  render(<Settings onJob={vi.fn()} />);
  const slider = screen.getByRole("slider", { name: /microphone sensitivity/i });
  await waitFor(() => expect(slider).toHaveValue("27"));
  fireEvent.change(slider, { target: { value: "65" } });
  expect(setVolume).not.toHaveBeenCalled();
  await act(async () => fireEvent.pointerUp(slider));
  expect(setVolume).toHaveBeenLastCalledWith(65);
  expect(screen.getByRole("status")).toHaveTextContent("65%");
  fireEvent.change(slider, { target: { value: "66" } });
  await act(async () => fireEvent.keyUp(slider, { key: "ArrowRight" }));
  expect(setVolume).toHaveBeenLastCalledWith(66);
  expect(slider).toHaveAttribute("max", "120");
  fireEvent.change(slider, { target: { value: "120" } });
  await act(async () => fireEvent.keyUp(slider, { key: "End" }));
  expect(setVolume).toHaveBeenLastCalledWith(120);
});

it("reports unavailable system control without pretending a value was saved", async () => {
  vi.spyOn(apiModule, "api").mockResolvedValue({ has_key: false });
  (window as any).mynotes = { getMicrophoneVolume: vi.fn().mockRejectedValue(new Error("PipeWire unavailable")) };
  render(<Settings onJob={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("PipeWire unavailable"));
  expect(screen.getByRole("slider", { name: /microphone sensitivity/i })).toBeDisabled();
});

it("rejects when backend readback mismatches the requested volume and shows error", async () => {
  vi.spyOn(apiModule, "api").mockResolvedValue({ has_key: false });
  (window as any).mynotes = {
    getMicrophoneVolume: vi.fn().mockResolvedValue({ volume: 50, muted: false }),
    setMicrophoneVolume: vi.fn().mockRejectedValue(new Error("Microphone volume did not update: requested 80% but system reported a different value.")),
  };
  render(<Settings onJob={vi.fn()} />);
  const slider = screen.getByRole("slider", { name: /microphone sensitivity/i });
  await waitFor(() => expect(slider).toHaveValue("50"));
  fireEvent.change(slider, { target: { value: "80" } });
  await act(async () => fireEvent.pointerUp(slider));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Microphone volume did not update"));
  expect(slider).toHaveValue("50");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("drops duplicate in-flight setMicrophoneVolume calls", async () => {
  vi.spyOn(apiModule, "api").mockResolvedValue({ has_key: false });
  let resolvePending: (v: { volume: number; muted: boolean }) => void;
  const pending = new Promise<{ volume: number; muted: boolean }>(r => { resolvePending = r; });
  const setMicSpy = vi.fn().mockReturnValue(pending);
  (window as any).mynotes = {
    getMicrophoneVolume: vi.fn().mockResolvedValue({ volume: 30, muted: false }),
    setMicrophoneVolume: setMicSpy,
  };
  render(<Settings onJob={vi.fn()} />);
  const slider = screen.getByRole("slider", { name: /microphone sensitivity/i });
  await waitFor(() => expect(slider).toHaveValue("30"));
  fireEvent.change(slider, { target: { value: "55" } });
  await act(async () => fireEvent.pointerUp(slider));
  await act(async () => fireEvent.pointerUp(slider));
  await act(async () => fireEvent.keyUp(slider, { key: "ArrowRight" }));
  expect(setMicSpy).toHaveBeenCalledTimes(1);
  resolvePending!({ volume: 55, muted: false });
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("55%"));
});
