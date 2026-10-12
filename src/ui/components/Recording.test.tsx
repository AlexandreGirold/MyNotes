import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Recording from "./Recording";
import { api } from "../api";

vi.mock("../api", () => ({ api: vi.fn() }));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("saves the delayed final recorder chunk before finishing and uploading", async () => {
  const order: string[] = [];
  class Recorder extends EventTarget {
    static isTypeSupported() { return true; }
    state = "inactive";
    ondataavailable?: (event: { data: Blob }) => void;
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      setTimeout(() => {
        const data = new Blob(["abc"]);
        Object.defineProperty(data, "arrayBuffer", { value: async () => new Uint8Array([1, 2, 3]).buffer });
        this.ondataavailable?.({ data });
        this.dispatchEvent(new Event("stop"));
      }, 80);
    }
  }
  vi.stubGlobal("MediaRecorder", Recorder);
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
  } });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  window.mynotes = {
    connection: vi.fn(), selectImport: vi.fn(),
    saveRecordingChunk: vi.fn(async (_id, index, bytes) => {
      expect(index).toBe(0); expect([...new Uint8Array(bytes)]).toEqual([1, 2, 3]);
      order.push("chunk");
    }),
    finishRecording: vi.fn(async () => {
      order.push("finish"); return { name: "recording.webm", data: new Uint8Array([1, 2, 3]).buffer };
    }),
  };
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/recordings") { order.push("upload"); return { id: "job", status: "queued" }; }
    return { id: "job", status: "succeeded", result: {} };
  });
  const view = render(<Recording onJob={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Start Recording" }));
  fireEvent.click(await screen.findByRole("button", { name: "Stop & Submit" }));
  await waitFor(() => expect(order).toEqual(["chunk", "finish", "upload"]));
  view.unmount();
});
