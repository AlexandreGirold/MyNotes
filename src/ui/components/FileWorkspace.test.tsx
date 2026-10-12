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
afterEach(() => vi.restoreAllMocks());

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
