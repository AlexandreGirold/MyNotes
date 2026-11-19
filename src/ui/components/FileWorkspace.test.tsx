import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FileWorkspace from "./FileWorkspace";
import * as apiModule from "../api";

const api = vi.fn();
beforeEach(() => {
  localStorage.clear(); api.mockReset(); vi.spyOn(apiModule, "api").mockImplementation(api);
  api.mockImplementation(async (path, init) => {
    if (init?.method === "PUT") return { path: "/allowed/note.md", sha256: "newhash" };
    if (path.startsWith("/workspace/text")) return { path: "/allowed/note.md", text: "Original", sha256: "oldhash" };
    return { entries: path.includes("path=&") ? [{ name: "allowed", path: "/allowed", directory: true }] : [{ name: "note.md", path: "/allowed/note.md", directory: false, editable: true }], next_offset: null };
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); delete (window as any).mynotes; });

it("browses canonical files and recovers unsaved edits without silently changing the file", async () => {
  const { unmount } = render(<FileWorkspace />);
  const folder = await screen.findByRole("button", { name: /allowed/ });
  await act(async () => fireEvent.click(folder));
  const entry = await screen.findByRole("button", { name: /note.md/ });
  await act(async () => fireEvent.click(entry));
  await waitFor(() => expect(screen.getByLabelText("Laptop file content")).toHaveValue("Original"));
  fireEvent.change(screen.getByLabelText("Laptop file content"), { target: { value: "Recovery edit" } });
  expect(screen.getByRole("status")).toHaveTextContent("not saved");
  expect(api.mock.calls.some(c => c[1]?.method === "PUT")).toBe(false);
  unmount(); render(<FileWorkspace />);
  fireEvent.click(screen.getByRole("button", { name: "note.md" }));
  expect(screen.getByLabelText("Laptop file content")).toHaveValue("Recovery edit");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save to original file" })));
  const saved = api.mock.calls.find(c => c[1]?.method === "PUT");
  expect(JSON.parse(saved![1].body)).toEqual({ path: "/allowed/note.md", text: "Recovery edit", expected_sha256: "oldhash" });
  expect(screen.getByRole("status")).toHaveTextContent("Saved to /allowed/note.md");
});

it("retains the recovery draft when the original changed externally", async () => {
  localStorage.setItem("mynotes-laptop-file-drafts", JSON.stringify({ "/allowed/note.md": { path: "/allowed/note.md", text: "Unsaved", sha256: "oldhash" } }));
  api.mockImplementation(async (path, init) => {
    if (init?.method === "PUT") throw new Error("409 File changed externally");
    return { entries: [], next_offset: null };
  });
  render(<FileWorkspace />);
  fireEvent.click(screen.getByRole("button", { name: "note.md" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save to original file" })));
  expect(screen.getByRole("alert")).toHaveTextContent("409");
  expect(screen.getByLabelText("Laptop file content")).toHaveValue("Unsaved");
  expect(localStorage.getItem("mynotes-laptop-file-drafts")).toContain("Unsaved");
});

it("adds folders directly without a provider connection and creates a note on disk", async () => {
  (window as any).mynotes = { selectDirectories: vi.fn().mockResolvedValue(["/new-root"]) };
  api.mockImplementation(async (path, init) => {
    if (path === "/settings") return { agent_directories: ["/allowed"] };
    if (init?.method === "PUT") return { path: "/new-root/Ideas.md", sha256: "created" };
    return { entries: [{ name: "new-root", path: "/new-root", directory: true }], next_offset: null };
  });
  render(<FileWorkspace />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Add folder…" })));
  const settings = api.mock.calls.find(c => c[0] === "/settings" && c[1]?.method === "PUT");
  expect(JSON.parse(settings![1].body)).toEqual({ agent_directories: ["/allowed", "/new-root"] });
  fireEvent.change(screen.getByLabelText("New file name"), { target: { value: "Ideas" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create note" })));
  const created = api.mock.calls.find(c => c[0] === "/workspace/text" && c[1]?.method === "PUT");
  expect(JSON.parse(created![1].body)).toEqual({ path: "/new-root/Ideas.md", text: "", expected_sha256: null });
  expect(screen.getByLabelText("Laptop file content")).toHaveValue("");
});

it("keeps edits typed during an autosave and uses the returned hash on the next save", async () => {
  let complete: (value: any) => void;
  const save = new Promise(resolve => { complete = resolve; });
  const base = api.getMockImplementation()!;
  api.mockImplementation(async (path, init) => init?.method === "PUT" ? save : base(path, init));
  render(<FileWorkspace />);
  const folder = await screen.findByRole("button", { name: "allowed" });
  await act(async () => fireEvent.click(folder));
  const entry = await screen.findByRole("button", { name: "note.md" });
  await act(async () => fireEvent.click(entry));
  await screen.findByLabelText("Laptop file content");
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole("checkbox", { name: "Autosave to original file" }));
  fireEvent.change(screen.getByLabelText("Laptop file content"), { target: { value: "First edit" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(api.mock.calls.filter(c => c[1]?.method === "PUT")).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("Laptop file content"), { target: { value: "Second edit while saving" } });
  await act(async () => complete!({ path: "/allowed/note.md", sha256: "newhash" }));
  expect(screen.getByLabelText("Laptop file content")).toHaveValue("Second edit while saving");
  expect(JSON.parse(localStorage.getItem("mynotes-laptop-file-drafts")!)["/allowed/note.md"]).toMatchObject({ text: "Second edit while saving", sha256: "newhash" });
  fireEvent.click(screen.getByRole("checkbox", { name: "Autosave to original file" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save to original file" })));
  const saves = api.mock.calls.filter(c => c[1]?.method === "PUT"), last = saves[saves.length - 1];
  expect(JSON.parse(last[1].body)).toMatchObject({ text: "Second edit while saving", expected_sha256: "newhash" });
  expect(api.mock.calls.filter(c => c[0].startsWith("/workspace/entries"))).toHaveLength(2);
});

it("stops autosave on a conflict and retains the latest draft", async () => {
  const base = api.getMockImplementation()!;
  api.mockImplementation(async (path, init) => { if (init?.method === "PUT") throw new Error("409 File changed externally"); return base(path, init); });
  render(<FileWorkspace />);
  const folder = await screen.findByRole("button", { name: "allowed" });
  await act(async () => fireEvent.click(folder));
  const entry = await screen.findByRole("button", { name: "note.md" });
  await act(async () => fireEvent.click(entry));
  vi.useFakeTimers();
  fireEvent.click(screen.getByRole("checkbox", { name: "Autosave to original file" }));
  fireEvent.change(screen.getByLabelText("Laptop file content"), { target: { value: "Keep this note" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  expect(screen.getByRole("alert")).toHaveTextContent("409");
  expect(screen.getByRole("checkbox", { name: "Autosave to original file" })).not.toBeChecked();
  expect(localStorage.getItem("mynotes-laptop-file-drafts")).toContain("Keep this note");
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(api.mock.calls.filter(c => c[1]?.method === "PUT")).toHaveLength(1);
});

it("lets the user review an external edit before saving the retained draft", async () => {
  let reads = 0;
  const base = api.getMockImplementation()!;
  api.mockImplementation(async (path, init) => {
    if (path.startsWith("/workspace/text") && !init) return { path: "/allowed/note.md", text: ++reads === 1 ? "Original" : "External edit", sha256: reads === 1 ? "oldhash" : "externalhash" };
    if (init?.method === "PUT") {
      if (JSON.parse(init.body).expected_sha256 !== "externalhash") throw new Error("409 File changed externally");
      return { path: "/allowed/note.md", sha256: "saved" };
    }
    return base(path, init);
  });
  render(<FileWorkspace />);
  const folder = await screen.findByRole("button", { name: "allowed" });
  await act(async () => fireEvent.click(folder));
  const entry = await screen.findByRole("button", { name: "note.md" });
  await act(async () => fireEvent.click(entry));
  fireEvent.change(screen.getByLabelText("Laptop file content"), { target: { value: "My retained draft" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save to original file" })));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Review disk version" })));
  expect(screen.getByLabelText("Current disk version")).toHaveValue("External edit");
  expect(screen.getByLabelText("Laptop file content")).toHaveValue("My retained draft");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save my draft over reviewed version" })));
  expect(screen.getByRole("status")).toHaveTextContent("Saved to");
  expect(localStorage.getItem("mynotes-laptop-file-drafts")).not.toContain("My retained draft");
});

it("deduplicates repeated pagination clicks while a folder request is in flight", async () => {
  let resolvePage: (value: any) => void;
  const nextPage = new Promise(resolve => { resolvePage = resolve; });
  api.mockImplementation(async path => {
    if (path.includes("offset=200")) return nextPage;
    return { entries: path.includes("path=&") ? [{ name: "allowed", path: "/allowed", directory: true }] : [], next_offset: path.includes("path=&") ? null : 200 };
  });
  render(<FileWorkspace />);
  const folder = await screen.findByRole("button", { name: "allowed" });
  await act(async () => fireEvent.click(folder));
  const more = await screen.findByRole("button", { name: "Load more files" });
  fireEvent.click(more); fireEvent.click(more);
  expect(api.mock.calls.filter(c => c[0].includes("offset=200"))).toHaveLength(1);
  await act(async () => resolvePage!({ entries: [{ name: "second.md", path: "/allowed/second.md", directory: false, editable: true }], next_offset: null }));
  expect(screen.getAllByRole("button", { name: "second.md" })).toHaveLength(1);
});
