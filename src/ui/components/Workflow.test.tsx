import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import LiveNotes from "./LiveNotes";
import ThemeToggle from "./ThemeToggle";
import * as apiMod from "../api";

const mockApi = vi.fn();
const mockSave = vi.fn();

describe("LiveNotes workflow", () => {
  const settings = { has_key: false, realtime_model: "voxtral", audio_summary_model: "gpt-oss:120b" };
  beforeEach(() => {
    vi.spyOn(apiMod, "api").mockImplementation(mockApi);
    mockApi.mockResolvedValue(settings);
    (window as any).mynotes = { saveTextFile: mockSave };
  });
  afterEach(() => { vi.restoreAllMocks(); mockSave.mockReset(); });

  it("no API key → transcript editable", () => {
    render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    expect(screen.getByRole("textbox", { name: "Live transcript" })).toHaveValue("");
  });

  it("save transcript as… — .md format, no double extension", async () => {
    mockSave.mockResolvedValue({ path: "/tmp/m.md", name: "m.md" });
    render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    const ta = screen.getByLabelText("Live transcript");
    await act(() => fireEvent.change(ta, { target: { value: "Hello" } }));
    await act(() => fireEvent.change(screen.getByLabelText("Session title"), { target: { value: "Meeting" } }));
    await act(() => fireEvent.click(screen.getByText("Save transcript as…")));
    expect(mockSave).toHaveBeenCalledWith({ name: "Meeting", text: "Hello", format: "md" });
  });

  it("cancel → no saved; {path} → shows saved status", async () => {
    mockSave.mockResolvedValue(null);
    const { unmount } = render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    await act(() => fireEvent.change(screen.getByLabelText("Live transcript"), { target: { value: "x" } }));
    await act(() => fireEvent.click(screen.getByText("Save transcript as…")));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    unmount();

    mockSave.mockResolvedValue({ path: "/tmp/ok.md", name: "ok.md" });
    render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    await act(() => fireEvent.change(screen.getByLabelText("Live transcript"), { target: { value: "y" } }));
    await act(() => fireEvent.click(screen.getByText("Save transcript as…")));
    expect(screen.getByRole("status")).toHaveTextContent("/tmp/ok.md");
  });

  it("personal notes survive rerender when inactive", () => {
    const { unmount } = render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Personal notes"), { target: { value: "draft" } });
    unmount();
    render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    expect(screen.getByLabelText("Personal notes")).toHaveValue("draft");
  });

  it("formats, previews and exports personal notes as Markdown without changing the source", async () => {
    localStorage.clear(); mockSave.mockResolvedValue({ path: "/tmp/personal.md" });
    render(<LiveNotes active={false} onJob={vi.fn()} onSaved={vi.fn()} onSettings={vi.fn()} />);
    const editor = screen.getByLabelText("Personal notes") as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "Important" } });
    editor.setSelectionRange(0, 9);
    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    expect(editor).toHaveValue("**Important**");
    fireEvent.change(editor, { target: { value: "## Ideas\n- [ ] Research this\n**Important**" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview Markdown" }));
    expect(screen.getByRole("heading", { name: "Ideas" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Research this" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Edit Markdown" }));
    expect(screen.getByLabelText("Personal notes")).toHaveValue("## Ideas\n- [ ] Research this\n**Important**");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save notes as…" })));
    expect(mockSave.mock.calls[0][0].text).toBe("## Ideas\n- [ ] Research this\n**Important**");
    expect(mockSave.mock.calls[0][0].format).toBe("md");
  });
});

describe("ThemeToggle", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it("persists theme across remount", () => {
    const { unmount } = render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("light");
    fireEvent.click(screen.getByLabelText("Enable night mode"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("mynotes-theme")).toBe("dark");
    unmount();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
