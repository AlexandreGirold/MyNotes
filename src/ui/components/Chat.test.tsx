import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

import Chat from "./Chat";

vi.mock("../api", () => ({
  api: vi.fn((path: string) => {
    if (path === "/conversations") return Promise.resolve([]);
    if (path === "/settings")
      return Promise.resolve({ has_key: false, global_chat_model: "gpt-oss:120b" });
    return Promise.resolve(undefined);
  }),
}));

Element.prototype.scrollIntoView = vi.fn();
afterEach(() => { localStorage.clear(); vi.clearAllMocks(); });

describe("Chat", () => {
  it("shows banner when no API key; Add API key calls onSettings; Send disabled; mode=global", async () => {
    const onSettings = vi.fn();
    render(
      <Chat
        selectedIds={[]}
        onJob={vi.fn()}
        onSource={vi.fn()}
        onSettings={onSettings}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Connect your Cloud Temple account to use the assistant.")).toBeInTheDocument();
    });

    const addKeyBtn = screen.getByRole("button", { name: /add api key/i });
    fireEvent.click(addKeyBtn);
    expect(onSettings).toHaveBeenCalledTimes(1);

    const textarea = screen.getByRole("textbox", { name: /question/i });
    fireEvent.change(textarea, { target: { value: "hello" } });

    const sendBtn = screen.getByRole("button", { name: /send/i });
    expect(sendBtn).toBeDisabled();

    const taskSelect = screen.getByRole("combobox", { name: /chat task/i });
    expect(taskSelect).toHaveValue("global");
  });

  it("switches saved conversations, creates a separate chat and sends its ID", async () => {
    const first = "a".repeat(32), second = "b".repeat(32), third = "c".repeat(32);
    let conversations = [{ id: first, title: "First topic" }, { id: second, title: "Second topic" }];
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path === "/settings") return { has_key: true, chat_harness: false };
      if (path === "/conversations" && init?.method === "POST") { const created = { id: third, title: "New conversation" }; conversations = [created, ...conversations]; return created; }
      if (path === "/conversations") return conversations;
      if (path === `/chats?conversation_id=${first}`) return [{ id: "one", question: "First question", answer: "First answer", citations: [] }];
      if (path === `/chats?conversation_id=${second}`) return [{ id: "two", question: "Second question", answer: "Second answer", citations: [] }];
      if (path === `/chats?conversation_id=${third}`) return [];
      if (path === "/chat") return { id: "job" };
      if (path === "/jobs/job") return { id: "job", status: "succeeded", result: { answer: "Third answer" } };
    });
    render(<Chat selectedIds={[]} onJob={vi.fn()} onSource={vi.fn()} onSettings={vi.fn()} />);
    await screen.findByText("First answer");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Second topic" })));
    expect(screen.getByText("Second answer")).toBeInTheDocument();
    expect(screen.queryByText("First answer")).not.toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "New conversation" })));
    expect(screen.queryByText("Second answer")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Question" }), { target: { value: "Third question" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /Send/ })));
    expect(await screen.findByText("Third answer")).toBeInTheDocument();
    const sent = vi.mocked(api).mock.calls.find(([path]) => path === "/chat")!;
    expect(JSON.parse(sent[1]!.body as string).conversation_id).toBe(third);
    expect(localStorage.getItem("mynotes-active-conversation")).toBe(third);
  });

  it("allows explicit web research without OpenCode and renders clickable public sources", async () => {
    const id = "d".repeat(32);
    // The initial catalogue is empty; creation returns one record.
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path === "/settings") return { has_key: true, chat_harness: true, agent_available: false };
      if (path === "/conversations") return init?.method === "POST" ? { id, title: "New conversation" } : [];
      if (path === "/chat") return { id: "webjob" };
      if (path === "/jobs/webjob") return { id: "webjob", status: "succeeded", result: { answer: "A sourced fact", citations: [{ source_id: "https://example.com", title: "Public source" }] } };
    });
    render(<Chat selectedIds={[]} onJob={vi.fn()} onSource={vi.fn()} onSettings={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "New conversation" })).toBeEnabled());
    fireEvent.change(screen.getByRole("combobox", { name: "Chat task" }), { target: { value: "web" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Question" }), { target: { value: "Read https://example.com" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /Send/ })));
    expect(await screen.findByRole("link", { name: "↗ Public source" })).toHaveAttribute("href", "https://example.com");
    const sent = vi.mocked(api).mock.calls.find(([path]) => path === "/chat")!;
    expect(JSON.parse(sent[1]!.body as string)).toMatchObject({ mode: "web", harness: false, document_ids: [] });
  });

  it("deletes the selected discussion and switches to the remaining one", async () => {
    const first = "a".repeat(32), second = "b".repeat(32);
    vi.mocked(api).mockImplementation(async path => {
      if (path === "/settings") return { has_key: false };
      if (path === "/conversations") return [{ id: first, title: "Delete me" }, { id: second, title: "Keep me" }];
      if (path === `/chats?conversation_id=${first}`) return [{ id: "one", question: "First", answer: "Deleted answer", citations: [] }];
      if (path === `/chats?conversation_id=${second}`) return [{ id: "two", question: "Second", answer: "Retained answer", citations: [] }];
      return { deleted: first };
    });
    render(<Chat selectedIds={[]} onJob={vi.fn()} onSource={vi.fn()} onSettings={vi.fn()} />);
    await screen.findByText("Deleted answer");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Delete conversation: Delete me" })));
    expect(screen.queryByText("Deleted answer")).not.toBeInTheDocument();
    expect(screen.getByText("Retained answer")).toBeInTheDocument();
    expect(vi.mocked(api)).toHaveBeenCalledWith(`/conversations/${first}`, { method: "DELETE" });
    expect(localStorage.getItem("mynotes-active-conversation")).toBe(second);
  });
});
