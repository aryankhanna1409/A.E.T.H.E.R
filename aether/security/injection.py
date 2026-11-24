"""Prompt-injection defence in code, not just in the prompt.

1. neutralize(): untrusted text cannot close or open AETHER's data blocks
   (</documents>, <user_message>, chat-template tokens), so it cannot pose as
   the user or the system.
2. scan(): flags text that tries to instruct the assistant.
3. Taint: once flagged content has been read in a turn or goal, the tool
   registry makes every action except plain reading need the user's explicit
   approval, with the reason shown. Injected text can therefore at most cause
   a question, never a silent action or a silent data leak (web_fetch to an
   attacker URL is an action too).
"""
from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern] = {
    "override": re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|the system)\b"
                           r".{0,30}\b(instructions?|prompts?|rules?|guidelines|directions)", re.I | re.S),
    "role_hijack": re.compile(r"\b(you are now|from now on,? you (are|will)|act as (an?|the) (unrestricted|jailbroken|developer)|"
                              r"enter (developer|god|dan) mode|new (system )?instructions?:)", re.I),
    "prompt_probe": re.compile(r"\b(reveal|print|show|repeat|output)\b.{0,30}\b(system prompt|hidden instructions|your instructions)", re.I),
    "secrecy": re.compile(r"\b(do not|don'?t|never) (tell|inform|mention (this )?to|alert) the user\b", re.I),
    "exfiltration": re.compile(r"(\b(send|upload|post|email|forward|exfiltrate)\b.{0,60}\b(https?://|to [\w.+-]+@[\w-]+\.)|"
                               r"!\[[^\]]*\]\(https?://[^)]*[?&][^)]*=)", re.I | re.S),
    "tool_command": re.compile(r"\b(call|run|execute|invoke|use)\b.{0,20}\b(fs_delete|fs_move|fs_modify|fs_create|shell\s*\(|"
                               r"the shell tool|app_open|ui_click|ui_type|start_goal|remember\s*\(|the remember tool|call remember|web_fetch)", re.I),
    "template_tokens": re.compile(r"<\|(im_start|im_end|system|user|assistant|endoftext)\|>|\[/?INST\]|<</?SYS>>", re.I),
    "block_escape": re.compile(r"</?\s*(user_message|tool_output|documents|memory|web|known_facts|system|calendar)\s*>", re.I),
}

BLOCK_TAGS = re.compile(r"<(/?)\s*(user_message|tool_output|documents|memory|web|known_facts|system|source|calendar)\s*>", re.I)
TEMPLATE = re.compile(r"<\|([a-z_]+)\|>", re.I)

# Categories that only read; still allowed without an extra question while tainted.
SAFE_WHEN_TAINTED = {"fs.read", "fs.list", "fs.search", "kg", "optimize", "web.search"}
# Categories whose OUTPUT is untrusted and gets scanned.
UNTRUSTED_OUTPUT = {"fs.read", "fs.search", "web.search", "web.fetch", "screen.observe"}


def scan(text: str) -> list[str]:
    if not text:
        return []
    return [name for name, rx in PATTERNS.items() if rx.search(text)]


def neutralize(text: str) -> str:
    """Make data-block tags and chat-template tokens inert (visible, but not structural)."""
    if not text:
        return text
    text = BLOCK_TAGS.sub(lambda m: f"‹{m.group(1)}{m.group(2)}›", text)
    return TEMPLATE.sub(lambda m: f"‹|{m.group(1)}|›", text)
