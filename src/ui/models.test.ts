import { expect, it } from "vitest";
import { REALTIME_MODEL_ID, realtimeModels, selectRealtimeModel } from "./models";

it("replaces a missing Voxtral alias only with an exposed realtime model", () => {
  expect(selectRealtimeModel(["gpt-oss:120b", REALTIME_MODEL_ID], "voxtral")).toBe(REALTIME_MODEL_ID);
  expect(selectRealtimeModel(["gpt-oss:120b"], "voxtral")).toBeNull();
  expect(realtimeModels(["whisper-large-v3", "gpt-oss:120b", REALTIME_MODEL_ID])).toEqual([REALTIME_MODEL_ID]);
});

it("preserves an available selection and does not guess between multiple realtime models", () => {
  expect(selectRealtimeModel(["asr-realtime", "another-realtime"], "asr-realtime")).toBe(REALTIME_MODEL_ID);
  expect(selectRealtimeModel(["first-realtime", "second-realtime"], "voxtral")).toBeNull();
});

it("uses the documented realtime engine ID even when the public catalogue lists only its alias", () => {
  expect(selectRealtimeModel(["gpt-oss:120b", "voxtral"], "voxtral")).toBe(REALTIME_MODEL_ID);
  expect(selectRealtimeModel(["voxtral"], REALTIME_MODEL_ID)).toBe(REALTIME_MODEL_ID);
  expect(realtimeModels(["voxtral", REALTIME_MODEL_ID])).toEqual([REALTIME_MODEL_ID]);
});
