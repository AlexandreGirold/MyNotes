export const REALTIME_MODEL_ID = "mistralai/Voxtral-Mini-4B-Realtime-2602";
// Published Cloud Temple catalogue, checked 2026-10-09; speeds are provider targets, not local benchmarks.
export const CLOUD_TEMPLE_MODELS = [
  ["qwen3.6:27b", "Chat", 80, 1000000, "General reasoning, long documents, vision"],
  ["gpt-oss:120b", "Chat", 94, 120000, "Configurable reasoning"],
  ["llama3.3:70b", "Chat", 10, 132000, "Multilingual dialogue"],
  ["nemotron-3-super:120b", "Chat", 72, 1000000, "Agents and long reasoning"],
  ["qwen3-2507:235b", "Chat", 56, 200000, "Maths, code, logical reasoning"],
  ["mistral-small4:119b", "Chat", 100, 262144, "Vision, fast general use"],
  ["qwen3-2507-think:4b", "Chat", 55, 250000, "Compact reasoning"],
  ["qwen3.6:35b", "Code", 121, 1000000, "Agentic coding, vision; SWE-bench 73.4% (provider claim)"],
  ["qwen-coder-next:80b", "Code", 97, 250000, "Large-scale code generation"],
  ["qwen3-next:80b", "Code", 67, 250000, "Tools and structured reasoning"],
  ["devstral-small-2:24b", "Code", 33, 200000, "Code exploration and vision"],
  ["functiongemma:270m", "Functions", 40, 32768, "Function-call routing"],
  ["qwen3-vl:235b", "Vision", 24, 200000, "Complex visual reasoning"],
  ["qwen3-vl:30b", "Vision", 39, 250000, "OCR, graphics, video"],
  ["qwen3-vl:4b", "Vision", 57, 250000, "Compact vision and OCR"],
  ["gemma4:31b", "Vision", 59, 250000, "Multilingual vision and reasoning"],
  ["gemma4:12b-it-qat", "Vision", 42, 250000, "Intermediate vision and reasoning"],
  ["bge-m3:567m", "Embedding", 171, 8192, "Multilingual embeddings"],
  ["qwen3-embedding:4b", "Embedding", 0, 40000, "Document embeddings"],
  ["qwen3-embedding:8b", "Embedding", 0, 40000, "High-capacity embeddings"],
  ["qwen3-embedding:0.6b", "Embedding", 0, 32768, "Compact embeddings"],
  ["granite-embedding:278m", "Embedding", 196.3, 512, "Low-latency embeddings"],
  ["embeddinggemma:300m", "Embedding", 175, 2048, "Multilingual embeddings"],
  ["nvidia/llama-nemotron-rerank-vl-1b-v2", "Reranking", 0, 4096, "Document relevance"],
  ["qwen3-reranker:4b", "Reranking", 0, 4096, "Document relevance"],
  ["qwen3-reranker:0.6b", "Reranking", 0, 4096, "Compact reranking"],
  ["bge-reranker-large", "Reranking", 0, 512, "Multilingual reranking"],
  ["granite3-guardian:8b", "Security", 45, 8192, "Content filtering"],
  ["granite3-guardian:2b", "Security", 60, 8192, "Compact filtering"],
  ["translategemma:27b", "Translation", 17, 120000, "Translation in 55 languages"],
  ["voxtral", "Audio", 0, 32768, "Realtime transcription"],
  ["z-image:16b", "Image generation", 0, 0, "Image generation"],
] as const;
const engineId = (id: string) => ["voxtral", "asr-realtime"].includes(id) ? REALTIME_MODEL_ID : id;

export function realtimeModels(ids: string[]): string[] {
  return [...new Set(ids.filter(id => id === "voxtral" || /realtime|real-time/i.test(id)).map(engineId))];
}

export function selectRealtimeModel(ids: string[], configured: string): string | null {
  const available = realtimeModels(ids);
  if (available.includes(engineId(configured))) return engineId(configured);
  if (available.includes(REALTIME_MODEL_ID)) return REALTIME_MODEL_ID;
  return available.length === 1 ? available[0] : null;
}
