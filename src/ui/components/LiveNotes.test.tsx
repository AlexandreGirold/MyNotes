import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import LiveNotes from "./LiveNotes";
import * as apiMod from "../api";

const settings = { has_key: false, realtime_model: "voxtral", audio_summary_model: "gpt-oss:120b" };
const onJob = vi.fn();
const onSaved = vi.fn();
const onSettings = vi.fn();

const mockApi = vi.fn();
const mockGetConnection = vi.fn();

describe("LiveNotes", () => {
  beforeEach(() => {
    onJob.mockReset(); onSaved.mockReset(); onSettings.mockReset();
    mockApi.mockReset(); mockGetConnection.mockReset();
    vi.spyOn(apiMod, "api").mockImplementation(mockApi);
    vi.spyOn(apiMod, "getConnection").mockImplementation(mockGetConnection);
    mockApi.mockResolvedValue(settings);
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("no API key flow: connect button, transcript editing, save / summarize buttons", async () => {
    /* ── render ───────────────────────────────────────────────────── */
    render(
      <LiveNotes active onJob={onJob} onSaved={onSaved} onSettings={onSettings} />,
    );

    /* settings GET was called; getConnection must NOT have been called */
    expect(mockApi).toHaveBeenCalledWith("/settings");
    expect(mockGetConnection).not.toHaveBeenCalled();

    /* ── button: Connect Cloud Temple → ───────────────────────────── */
    const connectBtn = screen.getByRole("button", { name: /connect cloud temple/i });
    expect(connectBtn).toBeInTheDocument();

    await act(async () => { fireEvent.click(connectBtn); });
    expect(onSettings).toHaveBeenCalledTimes(1);
    expect(mockGetConnection).not.toHaveBeenCalled();

    /* ── textarea: type / paste a transcript ───────────────────────── */
    const textarea = screen.getByLabelText("Live transcript");
    expect(textarea).toHaveValue("");

    await act(async () => { fireEvent.change(textarea, { target: { value: "Hello world" } }); });
    expect(textarea).toHaveValue("Hello world");

    /* ── button states ────────────────────────────────────────────── */
    const saveBtn    = screen.getByRole("button", { name: "Save transcript as…" });
    const summaryBtn = screen.getByRole("button", { name: "Summarize transcript" });

    expect(saveBtn).not.toBeDisabled();   // text present, idle status
    expect(summaryBtn).toBeDisabled();    // no API key
  });
});
