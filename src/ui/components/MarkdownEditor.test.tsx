import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import MarkdownEditor from "./MarkdownEditor";

it("inserts Tab at the selection while leaving Shift+Tab for keyboard navigation", () => {
  const onChange = vi.fn();
  render(<MarkdownEditor value="abc" onChange={onChange} label="Note" />);
  const editor = screen.getByLabelText("Note") as HTMLTextAreaElement;
  editor.setSelectionRange(1, 2);
  expect(fireEvent.keyDown(editor, { key: "Tab" })).toBe(false);
  expect(onChange).toHaveBeenCalledWith("a\tc");
  onChange.mockClear();
  expect(fireEvent.keyDown(editor, { key: "Tab", shiftKey: true })).toBe(true);
  expect(onChange).not.toHaveBeenCalled();
});

it("bounds a large preview without truncating its editable source", () => {
  const value = Array.from({ length: 8000 }, (_, i) => `- Observation ${i}`).join("\n");
  const onChange = vi.fn();
  const { container } = render(<MarkdownEditor value={value} onChange={onChange} label="Note" />);
  fireEvent.click(screen.getByRole("button", { name: "Preview Markdown" }));
  expect(container.querySelectorAll(".document-bullet").length).toBe(500);
  expect(screen.getByText(/Preview limited/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit Markdown" }));
  expect(screen.getByLabelText("Note")).toHaveValue(value);
  expect(onChange).not.toHaveBeenCalled();
});
