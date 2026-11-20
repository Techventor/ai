"""What counts as "AI", and how influence is weighted.

Everything tunable lives here. Edit this file rather than the logic.
"""

from __future__ import annotations

# --------------------------------------------------------------- discovery --

# Tier 1 -- unambiguously AI/ML. Searched with the low star floor.
TOPICS_CORE = [
    # language models
    "llm", "large-language-models", "large-language-model", "generative-ai",
    "foundation-models", "transformers", "llms",
    # agents
    "ai-agents", "ai-agent", "agentic-ai", "autonomous-agents", "multi-agent",
    "mcp", "model-context-protocol", "tool-calling",
    # retrieval
    "rag", "retrieval-augmented-generation", "vector-database", "vector-search",
    "embeddings", "semantic-search",
    # training / adaptation
    "fine-tuning", "lora", "peft", "rlhf", "distributed-training",
    # inference / serving
    "llm-inference", "inference-engine", "quantization", "model-serving",
    "llm-serving",
    # ops / eval
    "llmops", "mlops", "prompt-engineering", "llm-evaluation", "guardrails",
    "ai-observability",
    # multimodal / generative media
    "multimodal", "vision-language-model", "diffusion-models",
    "stable-diffusion", "text-to-image", "text-to-video", "image-generation",
    "text-to-speech", "speech-recognition", "tts", "asr", "voice-cloning",
    # products / assistants
    "chatbot", "ai-assistant", "code-generation", "ai-coding", "copilot",
    # ecosystems
    "openai", "anthropic", "huggingface", "langchain", "ollama",
]

# Tier 2 -- adjacent or classic ML. Very broad, so searched with a higher
# star floor to keep the candidate pool meaningful.
TOPICS_ADJACENT = [
    "machine-learning", "deep-learning", "neural-network", "neural-networks",
    "computer-vision", "nlp", "natural-language-processing",
    "reinforcement-learning", "data-science", "speech",
    "pytorch", "tensorflow", "jax", "onnx", "cuda", "gpu",
]

# Free-text probes, matched against name + description + readme. These catch
# influential projects whose maintainers never bothered to add topics.
KEYWORD_PROBES = [
    "llm", "gpt", "ai agent", "agentic", "rag pipeline", "diffusion model",
    "transformer model", "llm inference", "fine-tune", "embeddings",
    "multimodal", "text-to-image", "speech recognition", "ai coding assistant",
    "vector database", "prompt engineering", "model context protocol",
]

# A keyword-matched repo must still look AI-ish. These are counted against
# topics + description; see score.relevance().
RELEVANCE_TERMS = [
    "llm", "gpt", "ai", "a.i.", "agent", "agentic", "machine learning",
    "deep learning", "neural", "transformer", "diffusion", "embedding",
    "rag", "retrieval-augmented", "inference", "fine-tun", "multimodal",
    "vision-language", "text-to-image", "text-to-speech", "speech-to-text",
    "model", "openai", "anthropic", "claude", "llama", "mistral", "qwen",
    "huggingface", "pytorch", "tensorflow", "prompt", "chatbot", "copilot",
    "vector database", "tokenizer", "quantization", "mcp",
]

# ------------------------------------------------------------- exclusions --

# Repos that accumulate stars but are not software. Kept in the data, moved
# into their own category, and hidden from the main lists unless asked for.
NON_SOFTWARE_PATTERNS = [
    "awesome-", "-awesome", "awesome_",
    "tutorial", "tutorials", "course", "courses", "roadmap", "curriculum",
    "interview", "cheatsheet", "cheat-sheet", "handbook", "cookbook",
    "learn-", "-examples", "demos", "papers", "paper-list", "reading-list",
    "resources", "guide", "guides", "100-days", "bootcamp", "from-scratch",
    "zero-to-", "教程", "面试", "学习",
    # Teaching repos that name their audience rather than the format.
    # `microsoft/generative-ai-for-beginners` reached the influence top ten
    # on stars alone before these were added.
    "for-beginners", "beginner", "-101", "crash-course", "masterclass",
    "study-", "notebooks", "exercises", "workshop", "教学", "入门",
]

# ------------------------------------------------------------ categories ---

# Topics are an SEO keyword dump: `huggingface/transformers` tags itself
# with speech-recognition, audio and a dozen model names, which lands it in
# generative-media when it is plainly a library. A maintainer's own one-line
# description is far higher-precision, so an explicit self-description as a
# framework/library/SDK is checked before the topic sweep and wins.
FRAMEWORK_SELFDESC = [
    "framework", "library", " sdk", "sdk ", "toolkit",
    "engineering platform", "developer platform",
]

# Checked in order, first match wins, so SPECIFIC categories must come
# before GENERAL ones. Getting this order wrong is the main way the
# classifier goes bad: an early category with a broad term like "model"
# swallows half the pool. Keep broad terms out of the early entries, and
# keep the catch-alls (model-weights, app/ui, framework/sdk) last.
#   (label, topic_terms, text_terms)
CATEGORIES = [
    ("learning/list", NON_SOFTWARE_PATTERNS, ["awesome list", "curated list",
                                              "tutorial", "course", "roadmap"]),
    ("inference/serving",
     ["llm-inference", "inference-engine", "inference", "quantization",
      "model-serving", "llm-serving", "triton", "ollama", "llama-cpp",
      "local-llm", "edge-inference", "vllm", "tensorrt"],
     ["inference engine", "inference and serving", "throughput", "kv cache",
      "quantiz", "run llms locally", "run large language models",
      "get up and running with", "on your own hardware", "locally"]),
    ("rag/retrieval",
     ["rag", "retrieval-augmented-generation", "vector-database",
      "vector-search", "embeddings", "semantic-search", "vector-store"],
     ["vector database", "retrieval-augmented", "semantic search",
      "embedding store", "rag pipeline"]),
    ("training/tuning",
     ["fine-tuning", "lora", "peft", "rlhf", "distributed-training",
      "pretraining", "post-training"],
     ["fine-tuning", "fine tune", "training framework", "post-training",
      "rlhf", "reinforcement learning from human"]),
    ("generative-media",
     ["diffusion-models", "stable-diffusion", "text-to-image",
      "text-to-video", "image-generation", "video-generation",
      "text-to-speech", "tts", "asr", "speech-recognition", "voice-cloning",
      "image-editing"],
     ["image generation", "video generation", "text-to-speech",
      "speech recognition", "voice clon", "diffusion model"]),
    ("dev-tools", ["ai-coding", "code-generation", "copilot",
                   "coding-agent", "code-review"],
     ["coding agent", "code assistant", "ai pair", "autocomplete",
      "code review", "in your terminal", "cli agent"]),
    ("ops/eval",
     ["llmops", "mlops", "llm-evaluation", "evaluation", "guardrails",
      "ai-observability", "prompt-engineering", "observability"],
     ["observability", "evaluation framework", "benchmark suite", "tracing",
      "guardrail", "monitoring"]),
    ("agent",
     ["ai-agents", "ai-agent", "agentic-ai", "autonomous-agents",
      "multi-agent", "mcp", "model-context-protocol", "tool-calling"],
     ["agent framework", "autonomous agent", "multi-agent", "tool use",
      "computer use", "browser agent", "agentic workflow"]),
    # Deliberately narrow: this is for actual weight/checkpoint releases,
    # not for every tool that happens to mention Llama or Qwen.
    ("model-weights",
     ["pretrained-model", "pretrained-models", "model-weights", "checkpoint",
      "foundation-models", "model-zoo"],
     ["model weights", "we release", "we introduce", "model card",
      "technical report", "state-of-the-art model", "open-weight"]),
    ("app/ui", ["chatbot", "ai-assistant", "webui", "web-ui", "desktop-app",
                "gui"],
     ["chat ui", "web ui", "desktop app", "self-hosted chat", "frontend",
      "user interface"]),
    ("framework/sdk", ["langchain", "sdk", "framework", "api", "library",
                       "orchestration"],
     ["framework", "sdk", "toolkit", "library", "client for", "platform"]),
]

DEFAULT_CATEGORY = "other"

# ---------------------------------------------------------------- scoring --

# Ranking A: durable influence. "Which projects actually matter."
WEIGHTS_INFLUENCE = {
    "stars":        0.26,   # log-scaled popularity
    "community":    0.18,   # contributors (falls back to forks)
    "alive":        0.16,   # how recently it was pushed
    "momentum":     0.14,   # recent star delta
    "forks":        0.11,   # people building on it, not just bookmarking
    "maturity":     0.08,   # license + releases + org-owned + discussions
    "endurance":    0.07,   # age, i.e. it did not just spike and die
}

# Ranking B: momentum. "What is breaking out right now."
WEIGHTS_MOMENTUM = {
    "abs_growth":   0.40,   # stars gained per week, absolute
    "rel_growth":   0.25,   # that growth as a share of total stars
    "accel":        0.15,   # growth now vs growth in the previous window
    "alive":        0.12,
    "community":    0.08,
}

# Push-recency half-life in days: alive = 0.5 ** (days_since_push / HALF_LIFE)
ALIVE_HALF_LIFE_DAYS = 30.0

# --------------------------------------------------------------- hygiene ---

# A real project attracts forks. Below this fork:star ratio, popularity is
# suspect (bought stars, or a bookmark-only list).
MIN_HEALTHY_FORK_RATIO = 0.010

FLAG_RULES = {
    "suspect_stars_min":   5_000,   # apply the fork-ratio test above this
    "solo_stars_min":      3_000,   # many stars, <=2 contributors
    "dormant_days":        365,     # no push in this long
    "too_new_days":        21,      # too early to judge
    "issue_backlog_ratio": 0.08,    # open issues / stars
}

# Contributor counts at or above this are treated as "many", not as a
# number. GitHub resolves only the first 500 author emails to accounts and
# this call drops anonymous ones, so the count saturates below 500 however
# large the project is. Scoring the raw figure ordered the top of the
# ranking by which addresses happened to resolve.
CONTRIBUTOR_SATURATION = 400

# ---------------------------------------------------------------- limits ---

DEFAULTS = {
    "min_stars_core":     300,
    "min_stars_adjacent": 3_000,
    "min_stars_keyword":  1_000,
    "search_page_size":   100,
    "search_min_interval": 2.1,     # 30 search req/min -> one every 2.1s
    "enrich_top_n":       250,
    "enrich_budget_s":    1200,     # stage 2 stops after 20 minutes
    "max_per_owner":      3,
    "report_top_n":       40,
}
