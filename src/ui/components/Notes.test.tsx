import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Notes from "./Notes";

function props() {
  return {
    selectedIds: [], onJob: vi.fn(), jobs: {}, noteFormat: "markdown" as const,
    noteTitle: "My note", noteContent: "<script>evil()</script>", noteCourse: "",
    setNoteFormat: vi.fn(), setNoteTitle: vi.fn(), setNoteContent: vi.fn(), setNoteCourse: vi.fn(),
    selectedDoc: null, version: null, saveNote: vi.fn().mockResolvedValue({}),
    updateNote: vi.fn().mockResolvedValue({}), loadDocumentDetails: vi.fn(), toggleSelect: vi.fn(),
    documents: [], searchQuery: "", setSearchQuery: vi.fn(), doSearch: vi.fn(),
    searchResults: [], searchLoading: false, courses: [], apiBase: "", downloadFile: vi.fn(),
  };
}

describe("Notes integration", () => {
  it("saves the current note through the supplied backend action", async () => {
    const p = props();
    render(<Notes {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Recovery library & imports" }));
    fireEvent.click(screen.getByRole("button", { name: "Save recovery copy" }));
    await waitFor(() => expect(p.saveNote).toHaveBeenCalledWith("My note", p.noteContent, "markdown", undefined));
    expect(p.updateNote).not.toHaveBeenCalled();
  });

  it("surfaces a failed save", async () => {
    const p = props();
    p.saveNote.mockRejectedValue(new Error("Backend unavailable"));
    render(<Notes {...p} />);
    fireEvent.click(screen.getByRole("button", { name: "Recovery library & imports" }));
    fireEvent.click(screen.getByRole("button", { name: "Save recovery copy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unavailable");
  });

  it("previews imported markup as text without executing it", () => {
    render(<Notes {...props()} />);
    fireEvent.click(screen.getByRole("button", { name: "Recovery library & imports" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    const preview = screen.getByRole("region", { name: "Preview" });
    expect(preview).toHaveTextContent("<script>evil()</script>");
    expect(preview.querySelector("script")).toBeNull();
  });
});
