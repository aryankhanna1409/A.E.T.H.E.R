"""Configuration. Everything tunable lives in `aether.toml` so the optimizer
can evolve it and you can inspect it. No API keys exist anywhere: every model
runs in-process on your machine."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


def default_home() -> Path:
    base = os.environ.get("AETHER_HOME")
    if base:
        return Path(base)
    if os.name == "nt":  # Windows 11: %LOCALAPPDATA%\AETHER
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AETHER"
    return Path.home() / ".aether"


class LLMConfig(BaseModel):
    backend: str = "llama_cpp"  # "llama_cpp" | "transformers"
    model_path: str = ""  # path to a .gguf file (llama_cpp) or HF folder (transformers)
    # Defaults fit Aryan's HP Victus 15 (RTX 3050 Laptop 4 GB, Ryzen 5 5600H, 16 GB RAM)
    # running Qwen2.5-7B Q4_K_M: ~20 of 28 layers on the GPU, the rest on the CPU.
    # Run `python scripts/tune_gpu.py` to measure the best value on your machine.
    context_tokens: int = 6144
    gpu_layers: int = 20  # -1 = offload everything; 0 = CPU only
    threads: int = 6  # physical cores; hyperthreads slow llama.cpp down
    batch_size: int = 256  # prompt-processing batch; smaller saves VRAM
    flash_attn: bool = True  # less VRAM for the KV cache on RTX cards
    temperature: float = 0.6
    max_reply_tokens: int = 1024
    chat_format: str | None = None  # None = read template from the GGUF
    prompt_cache_mb: int = 768  # reuse the KV cache for the shared prompt prefix (RAM); 0 = off
    # Speculative decoding by prompt lookup: guesses the next tokens from text already in the prompt
    # (retrieved notes, quoted facts) and verifies them in one batch. No extra model or VRAM.
    # Run `python scripts/bench.py --compare-draft` to see whether it helps on your machine; 0 = off.
    draft_lookup_tokens: int = 0


class EmbeddingConfig(BaseModel):
    backend: str = "sentence_transformers"  # or "lexical" (no model; BM25 only)
    model: str = "BAAI/bge-small-en-v1.5"  # downloaded once, then fully offline
    device: str = "cpu"


class MemoryConfig(BaseModel):
    rag_min_score: float = 0.25
    rag_top_k: int = 6
    hybrid_alpha: float = 0.6  # weight of vector score vs BM25 in hybrid search
    dedupe_threshold: float = 0.92
    reranker_model: str = ""  # e.g. "BAAI/bge-reranker-base" (CPU, ~1 GB RAM); empty = off
    watch_interval_s: int = 300  # re-scan indexed folders this often (0 = off); only changed files are re-read
    # Folders indexed for search. Empty = every readable folder. System, program, cache and hidden folders
    # ($Recycle.Bin, Windows, Program Files, AppData, .venv, node_modules, .git, ...) are always skipped.
    index_roots: list[str] = []
    episodic_keep_days: int = 365


class ContextConfig(BaseModel):
    topic_switch_threshold: float = 0.12
    working_memory_turns: int = 12


class ProactiveConfig(BaseModel):
    enabled: bool = True
    quiet_start: str = "23:00"  # nothing is shown between these times; it waits
    quiet_end: str = "07:30"
    max_per_hour: int = 3
    morning_brief: str = "08:30"  # empty = off
    wellness_checkins: bool = True
    check_interval_s: int = 60
    event_alert_minutes: int = 15  # "Lecture in 15 minutes, Room 4" (0 = off)
    evening_review: str = "20:00"  # what's still open today + tomorrow's first event ("" = off)
    low_battery: int = 20  # warn when on battery and at or below this percent (0 = off)
    focus_break_minutes: int = 90  # suggest a break after this long at the keyboard without one (0 = off)
    deadline_nudges: bool = True  # deadlines from the calendar, reminders and indexed documents, nudged early
    deadline_lead_days: list[int] = Field(default_factory=lambda: [7, 3, 1])


class OnlineConfig(BaseModel):
    # Live information with no accounts or API keys. Only the place name / topic leaves the PC.
    # The whole thing follows security.network: turn that off and AETHER is fully offline again.
    location: str = ""  # e.g. "Vellore" or "Vellore, India" or "12.97,79.16"; used for weather
    news_feeds: list[str] = Field(default_factory=lambda: [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "https://feeds.arstechnica.com/arstechnica/index",
    ])
    news_region: str = "IN"  # for topic searches ("news about robotics")
    briefing_headlines: int = 3


class BriefingConfig(BaseModel):
    on_first_greeting: bool = True  # the first "hey jarvis" / "good morning" of the morning gives the briefing
    window_start: str = "04:30"  # "morning" for the greeting trigger
    window_end: str = "12:00"


class MailConfig(BaseModel):
    # Folders of .eml files or .mbox archives already on this PC (Outlook: drag mail into the folder;
    # Thunderbird: its profile's Mail folder; Gmail: Google Takeout). Read-only; nothing is fetched or sent.
    paths: list[str] = Field(default_factory=lambda: [str(Path.home() / "Documents" / "AETHER" / "mail")])
    outlook: bool = False  # also read the classic Outlook desktop app's inbox on this PC (needs pywin32)
    drafts: str = ""  # where reply drafts go; empty = a "drafts" folder inside the first mail folder
    vip: list[str] = Field(default_factory=list)  # names or addresses whose new mail is worth a notification


class CalendarConfig(BaseModel):
    # Folders or .ics files exported from Outlook, Google Calendar, Apple Calendar, a timetable...
    # Read locally; nothing is synced online. Events AETHER adds go to aether.ics in the first folder.
    ics_paths: list[str] = Field(default_factory=lambda: [str(Path.home() / "Documents" / "AETHER" / "calendar")])
    outlook: bool = False  # also read the classic Outlook desktop app on this PC (needs pywin32)
    day_start: str = "09:00"  # for finding free time
    day_end: str = "21:00"


class SecurityConfig(BaseModel):
    encrypt_at_rest: bool = True  # memories + conversation logs; key wrapped with Windows DPAPI
    allowed_read_roots: list[str] = Field(default_factory=lambda: [str(Path.home() / "Documents" / "AETHER")])
    allowed_write_roots: list[str] = Field(default_factory=list)  # empty = read-only to start
    auto_approve: list[str] = Field(default_factory=lambda: ["fs.read", "fs.list", "fs.search", "web.search", "web.fetch",
                                                              "web.open"])
    network: bool = True  # web research on/off
    # Apps AETHER may open: name -> command or full path to the .exe
    apps: dict[str, str] = Field(default_factory=lambda: {
        "notepad": "notepad.exe", "calculator": "calc.exe", "explorer": "explorer.exe",
        "vscode": "code", "chrome": "chrome.exe", "edge": "msedge.exe", "spotify": "spotify.exe",
    })
    # Programs the sandboxed shell may run (first word of the command). Always asks first.
    shell_allow: list[str] = Field(default_factory=lambda: ["python", "py", "pytest", "git", "node", "npm", "dir", "where"])
    shell_timeout_s: int = 120
    screen_access: bool = False  # screenshots + OCR; off until you turn it on
    ui_automation: bool = False  # keyboard/mouse control of apps; off until you turn it on
    denylist_globs: list[str] = Field(default_factory=lambda: [
        "*.env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.kdbx", "*.ppk",
        "Login Data", "Cookies", "*.sqlite-wal", "credentials*", "*secret*",
    ])
    denylist_dirs: list[str] = Field(default_factory=lambda: [
        ".ssh", ".aws", ".gnupg", ".azure", "AppData\\Roaming\\Microsoft\\Credentials",
        "AppData\\Local\\Google\\Chrome\\User Data", "AppData\\Local\\Microsoft\\Edge\\User Data",
    ])


class VoiceConfig(BaseModel):
    # Speech runs on the CPU so the 4 GB GPU stays free for the LLM.
    # small.en with the prompt, hotwords and your speech profile handles Indian English well on the CPU (~1 s).
    # medium.en is more accurate on strong accents but takes ~3 s per sentence on a Ryzen 5 5600H.
    # (On the GPU Whisper would need ~1 GB of VRAM that the 7B model is already using.)
    stt_model: str = "small.en"  # faster-whisper: tiny.en | base.en | small.en | medium.en
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_beam_size: int = 5        # beam search: slower than greedy but much better on accents
    stt_prompt: str = ""          # extra context for Whisper, e.g. "Robotics student. Terms: ROS 2, Nav2, SLAM."
    tts_voice_path: str = ""  # Piper voice .onnx, e.g. en_GB-alan-medium.onnx (see README)
    wake_word: str = "hey_jarvis"  # openWakeWord built-in model name, or a path to your own .onnx
    wake_threshold: float = 0.5
    wake_verifier_path: str = ""   # made by scripts/learn_my_voice.py: tunes "hey Jarvis" to your voice
    # Say the assistant's name instead: short phrases are checked with a tiny Whisper model ("hey Nova").
    wake_by_name: bool = True
    wake_stt_model: str = "tiny.en"
    silence_ms: int = 900  # end of utterance after this much silence
    vad_energy: float = 0.012  # RMS level that counts as speech; raise in noisy rooms
    barge_in: bool = True  # interrupt AETHER by speaking while it talks
    sample_rate: int = 16000


class VisionConfig(BaseModel):
    model_path: str = ""  # e.g. Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf
    mmproj_path: str = ""  # the matching mmproj-*.gguf
    handler: str = "qwen25vl"  # qwen25vl | llava15 | llava16 | minicpmv26 | moondream
    gpu_layers: int = 0  # CPU by default: the GPU is busy with the main LLM
    context_tokens: int = 4096


class Config(BaseModel):
    name: str = "AETHER"
    user_name: str = "Aryan"
    persona: str = "jarvis"  # jarvis | neutral
    address_as: str = ""  # e.g. "sir"; empty = use the name naturally
    home: Path = Field(default_factory=default_home)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    online: OnlineConfig = Field(default_factory=OnlineConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    max_plan_steps: int = 8

    @property
    def db_path(self) -> Path:
        return self.home / "aether.db"

    @property
    def audit_path(self) -> Path:
        return self.home / "audit.jsonl"


def load_config(path: str | Path | None = None) -> Config:
    # The install folder too, so it is found when Windows starts AETHER at sign-in from another folder.
    install = Path(__file__).resolve().parents[2] / "aether.toml"
    candidates = [Path(path)] if path else [Path("aether.toml"), install, default_home() / "aether.toml"]
    data: dict = {}
    for p in candidates:
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            break
    cfg = Config.model_validate(data)
    cfg.home = Path(cfg.home).expanduser()
    cfg.home.mkdir(parents=True, exist_ok=True)
    sec = cfg.security
    sec.allowed_read_roots = [str(Path(r).expanduser().resolve()) for r in sec.allowed_read_roots]
    sec.allowed_write_roots = [str(Path(r).expanduser().resolve()) for r in sec.allowed_write_roots]
    return cfg
