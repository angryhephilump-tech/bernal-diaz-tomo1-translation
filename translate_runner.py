#!/usr/bin/env python3
"""
translate_runner.py — batch literary translation via Cursor CLI (cursor-agent).

Each section is translated in a brand-new cursor-agent process (no context bleed).
Run --build-map once to freeze section boundaries, then run the default mode to translate.

QA gates (hard-fail + retry): bilingual word-ratio band, truncation detection, source
coverage, edition apparatus, Cristóbal/Olid name check, false flag claims.

Requires: cursor-agent on PATH (install: irm 'https://cursor.com/install?win32=true' | iex)
          cursor-agent login   (or set CURSOR_API_KEY)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

# =============================================================================
# CONFIG — edit these paths and settings before running
# =============================================================================

# Project folder — run all commands from here (or use absolute paths)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Input / output files
SOURCE_FILE = r"C:\Users\drewc\Downloads\Bernal_Diaz_part_1.txt"
UTP_FILE = os.path.join(PROJECT_DIR, "utp.txt")             # Translation instructions (Universal Translation Protocol)
MAP_FILE = os.path.join(PROJECT_DIR, "section_map.json")    # Frozen section map (written by --build-map)
OUTPUT_FILE = os.path.join(PROJECT_DIR, "translation_output.txt")
SKIP_FILE = os.path.join(PROJECT_DIR, "skipped_sections.txt")
LOWRATIO_FILE = os.path.join(PROJECT_DIR, "low_ratio_sections.txt")
VALIDATION_FAILED_FILE = os.path.join(PROJECT_DIR, "validation_failed_sections.txt")
PROBLEMS_FILE = os.path.join(PROJECT_DIR, "map_problems.txt")
VOICE_LOG_FILE = os.path.join(PROJECT_DIR, "voice_log.txt")

# Map building: "regex" = detect CAPITULO/INTRO headers in source (reliable for OCR editions)
#               "ai"    = cursor-agent boundary pass (original mode)
MAP_BUILD_MODE = "regex"

MAP_CHUNK_WORDS = 25000
# Overlap between mapping chunks so a section boundary on a seam is not lost
MAP_CHUNK_OVERLAP_WORDS = 300

# Bilingual parity: words(english) / words(spanish) must fall in this band or section is rejected
RATIO_THRESHOLD = 0.7
RATIO_MAX = 1.45

# English should not be far shorter than the source chunk (truncation tripwire)
SOURCE_EN_RATIO_MIN = 0.45

# Re-translate attempts when validation fails (same section, fresh cursor-agent call)
MAX_RETRIES = 2

# Sections larger than this are translated in two sequential calls (avoid output truncation)
MAX_SECTION_WORDS = 4000

# Cursor CLI model — Composer (cheap lane). Pin explicitly; do not rely on defaults.
MODEL = "composer-2.5"

# cursor-agent — invoke bundled node.exe directly (avoids PowerShell execution-policy issues)
CURSOR_AGENT_VERSION_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "cursor-agent", "versions", "2026.06.19-653a7fb"
)

# Seconds to wait for one cursor-agent call before treating it as failed
AGENT_TIMEOUT = 600

# Regex patterns for independent header cross-check (Bernal Díaz / García 1904 edition)
HEADER_PATTERNS = [
    r"(?m)^\s*INTRODUCCI[ÓO]N",
    r"(?m)^\s*\[CAPITULO",
    r"(?m)^\s*CAPITULO\s+",
    r"(?m)^\s*Capítulo\s+",
    r"(?m)^\s*CAPÍTULO\s+",
    r"(?m)^\s*PRE[ÁA]MBULO",
    r"(?m)^\s*NUMERO\s+\d+",
    r"(?m)^\s*TABLA DE VARIANTES",
    r"(?m)^\s*AP[ÉE]NDICE",
    r"(?m)^\s*[IVXLC]+\.\s*SU\s+",
    r"(?m)^\s*PRÓLOGO\s*$",
    r"(?m)^\s*PROLOGO\s*$",
]

# =============================================================================
# Internal constants (usually no need to edit)
# =============================================================================

REGEX_SECTION_HEADER = re.compile(
    r"(?m)^(?:Y\s+)?("
    r"\[CAPITULO[^\n]+|"
    r"CAPITULO\s+[A-Z\[Ñ][^\n]*"
    r")",
)

REGEX_PREAMBLE_START = re.compile(
    r"(?m)^OTANDO estado como los muy afamados", re.IGNORECASE
)

SECTION_LINE_RE = re.compile(
    r"^\s*(?P<heading>.+?)\s*\|\|\|\s*(?P<anchor>.+?)\s*$", re.MULTILINE
)
OUTPUT_SECTION_RE = re.compile(r"=== .+ — Section (\d+) ===")
OUTPUT_SECTION_BLOCK_RE = re.compile(
    r"=== (?P<heading>.+?) — Section (?P<n>\d+) ===\s*"
    r"<english>.*?</english>\s*"
    r"<spanish>.*?</spanish>\s*"
    r"<flags>(?P<flags>.*?)</flags>",
    re.DOTALL | re.IGNORECASE,
)
VOICE_LINE_RE = re.compile(r"^\s*voice:\s*(.+)\s*$", re.IGNORECASE)
WORD_RE = re.compile(r"\S+")
BLOCK_MARKERS = ("blocked", "content", "cannot")

# Edition apparatus to strip post-translation (García 1904 / Google scan patterns)
APPARATUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\*Bernal D[ií]az del Castillo\.—\d+\.\*", re.IGNORECASE),
    re.compile(r"---\s*Page\s+\d+\s*---", re.IGNORECASE),
    re.compile(r"Digitized by Google", re.IGNORECASE),
]

SENTENCE_END_RE = re.compile(r'(?:[.!?]["\'\)\]\u201d\u2019]?|—)\s*$')
TRUNCATION_END_RE = re.compile(r"[\(,;:\-]$")

OUTPUT_SECTION_PARSE_RE = re.compile(
    r"=== (?P<heading>.+?) — Section (?P<n>\d+) ===\s*"
    r"<english>(?P<english>.*?)</english>\s*"
    r"<spanish>(?P<spanish>.*?)</spanish>\s*"
    r"<flags>(?P<flags>.*?)</flags>",
    re.DOTALL | re.IGNORECASE,
)

IDENTITY_FLAG_RE = re.compile(r"(\S+)\s*→\s*\1\b(?:\s*\[[^\]]*\])?")
FALSE_OLID_FLAG_RE = re.compile(r"xpoual\s*→\s*Gonzalo", re.IGNORECASE)
GONZALO_OLID_RE = re.compile(r"Gonzalo de Olid|Gonzalo de Oli\b", re.IGNORECASE)
CRISTOBAL_OLID_RE = re.compile(r"Crist[oó]bal de Olid", re.IGNORECASE)
SOURCE_XPOUAL_OLID_RE = re.compile(r"xpoual\s+de\s+oli", re.IGNORECASE)


# ---------------------------------------------------------------------------
# File / text helpers
# ---------------------------------------------------------------------------


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def append_text(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def write_text(path: str, text: str) -> None:
    """Write atomically via temp file (handles Windows replace more reliably)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # Last resort: direct write (may fail if file is open exclusively in the IDE)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def word_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in WORD_RE.finditer(text)]


def slice_words(text: str, start_word: int, num_words: int) -> str:
    """Return exact source substring covering words [start_word, start_word + num_words)."""
    spans = word_spans(text)
    if start_word >= len(spans):
        return ""
    end_word = min(start_word + num_words, len(spans))
    return text[spans[start_word][0] : spans[end_word - 1][1]]


def chunk_source_by_words(text: str, chunk_words: int, overlap_words: int) -> list[str]:
    """Split source into overlapping chunks preserving original characters."""
    spans = word_spans(text)
    total = len(spans)
    if total == 0:
        return [text] if text else []

    chunks: list[str] = []
    start_word = 0
    while start_word < total:
        end_word = min(start_word + chunk_words, total)
        chunk = text[spans[start_word][0] : spans[end_word - 1][1]]
        chunks.append(chunk)
        if end_word >= total:
            break
        start_word = max(0, end_word - overlap_words)
    return chunks


def first_n_words(text: str, n: int = 8) -> str:
    spans = word_spans(text)
    if not spans:
        return ""
    take = min(n, len(spans))
    return text[spans[0][0] : spans[take - 1][1]]


def last_n_words(text: str, n: int = 8) -> str:
    spans = word_spans(text)
    if not spans:
        return ""
    take = min(n, len(spans))
    return text[spans[-take][0] : spans[-1][1]]


def find_verbatim(haystack: str, needle: str) -> int:
    """Return character offset of needle in haystack, or -1 if not found."""
    if not needle:
        return -1
    return haystack.find(needle)


def decode_ai_anchor(anchor: str) -> str:
    """Undo literal \\uXXXX escapes the model sometimes emits in anchors."""
    if "\\u" not in anchor and "\\U" not in anchor:
        return anchor
    try:
        return anchor.encode("utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return anchor


def find_anchor_offset(
    source: str, anchor: str, search_from: int = 0, min_words: int = 3
) -> tuple[int, str]:
    """
    Locate an AI anchor in source. Tries exact match, then trims trailing words,
    then flexible whitespace — needed for OCR/colonial text where the model
    may extend or slightly alter the 8-word anchor.
    Returns (offset, matched_substring) or (-1, anchor).
    """
    anchor = decode_ai_anchor(anchor).replace("\ufffd", "").strip()
    haystack = source[search_from:]
    if not anchor or not haystack:
        return -1, anchor

    pos = haystack.find(anchor)
    if pos >= 0:
        matched = anchor
        return search_from + pos, matched

    words = WORD_RE.findall(anchor)
    for n in range(len(words), min_words - 1, -1):
        sub = " ".join(words[:n])
        pos = haystack.find(sub)
        if pos >= 0:
            return search_from + pos, sub

    for n in range(len(words), min_words - 1, -1):
        pattern = r"\s+".join(re.escape(w) for w in words[:n])
        m = re.search(pattern, haystack, flags=re.IGNORECASE)
        if m:
            return search_from + m.start(), m.group(0)

    return -1, anchor


def resolve_cursor_agent_dir() -> str:
    """Return the cursor-agent version directory containing node.exe."""
    if (
        os.path.isfile(os.path.join(CURSOR_AGENT_VERSION_DIR, "node.exe"))
        and os.path.isfile(os.path.join(CURSOR_AGENT_VERSION_DIR, "index.js"))
    ):
        return CURSOR_AGENT_VERSION_DIR

    versions_root = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "cursor-agent", "versions"
    )
    if not os.path.isdir(versions_root):
        raise FileNotFoundError("cursor-agent not installed")

    candidates = []
    for name in os.listdir(versions_root):
        path = os.path.join(versions_root, name)
        if os.path.isfile(os.path.join(path, "node.exe")) and os.path.isfile(
            os.path.join(path, "index.js")
        ):
            candidates.append(path)
    if not candidates:
        raise FileNotFoundError("no cursor-agent version directory found")
    return sorted(candidates)[-1]


# ---------------------------------------------------------------------------
# cursor-agent invocation (verified flags from `cursor-agent --help`)
#   -p / --print          → non-interactive; model text on stdout
#   --model <model>       → model selection
#   -f / --force          → auto-approve tool use (no approval prompts)
#   --trust               → trust workspace without prompting (headless)
#   --output-format text  → plain text stdout
# ---------------------------------------------------------------------------


def call_cursor_agent(prompt: str) -> tuple[str, str | None]:
    """
    Run one fresh cursor-agent process. Returns (stdout, error_message).
    Invokes bundled node.exe directly — bypasses PowerShell .ps1 wrappers.
    """
    try:
        agent_dir = resolve_cursor_agent_dir()
    except FileNotFoundError as exc:
        return "", str(exc)

    node = os.path.join(agent_dir, "node.exe")
    index = os.path.join(agent_dir, "index.js")
    cmd = [
        node,
        index,
        "-p",
        "--output-format",
        "text",
        "--model",
        MODEL,
        "--trust",
        "-f",
        "agent",
    ]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AGENT_TIMEOUT,
            cwd=agent_dir,
        )
    except subprocess.TimeoutExpired:
        return "", f"timeout after {AGENT_TIMEOUT}s"
    except OSError as exc:
        return "", str(exc)

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        msg = stderr.strip() or f"exit code {result.returncode}"
        return stdout, msg
    return stdout, None


# ---------------------------------------------------------------------------
# Map building
# ---------------------------------------------------------------------------

MAP_PROMPT = """List every natural section/chapter you see in the source text below.
For each, give: the heading, and the FIRST 8 words copied EXACTLY and VERBATIM from the source —
character for character, do not correct, normalize, paraphrase, or fix OCR.

Output one section per line as:
HEADING ||| FIRST_8_WORDS

Do not include any other commentary.

SOURCE TEXT:
"""


def parse_section_lines(agent_output: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in agent_output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|||" not in line:
            continue
        m = SECTION_LINE_RE.match(line)
        if m:
            heading = m.group("heading").strip()
            anchor = m.group("anchor").strip()
            if heading and anchor:
                found.append((heading, anchor))
    return found


def collect_sections_from_ai(source: str) -> list[tuple[str, str]]:
    """AI boundary pass over chunked source; de-duplicate overlap duplicates by anchor."""
    chunks = chunk_source_by_words(source, MAP_CHUNK_WORDS, MAP_CHUNK_OVERLAP_WORDS)
    seen_anchors: set[str] = set()
    sections: list[tuple[str, str]] = []

    for i, chunk in enumerate(chunks, 1):
        print(f"  Mapping chunk {i}/{len(chunks)} ({word_count(chunk)} words)...")
        prompt = MAP_PROMPT + chunk
        out, err = call_cursor_agent(prompt)
        if err:
            print(f"  WARNING: chunk {i} cursor-agent error: {err}", file=sys.stderr)
        for heading, anchor in parse_section_lines(out):
            if anchor in seen_anchors:
                continue
            seen_anchors.add(anchor)
            sections.append((heading, anchor))
    return sections


def locate_sections(
    source: str, sections: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Find each anchor in source order (sequential search avoids duplicate hits)."""
    located: list[dict[str, Any]] = []
    search_from = 0
    for heading, anchor in sections:
        offset, matched = find_anchor_offset(source, anchor, search_from)
        located.append(
            {
                "heading": heading,
                "first_anchor": matched,
                "start_offset": offset,
            }
        )
        if offset >= 0:
            search_from = offset + max(1, len(matched))
    located.sort(key=lambda s: (s["start_offset"] if s["start_offset"] >= 0 else 10**18))
    return located


def regex_header_count(source: str) -> int:
    lines_seen: set[int] = set()
    for pattern in HEADER_PATTERNS:
        for m in re.finditer(pattern, source, flags=re.IGNORECASE):
            lines_seen.add(m.start())
    return len(lines_seen)


def finalize_section_records(
    source: str, located: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for i, sec in enumerate(located):
        start = sec["start_offset"]
        if i + 1 < len(located):
            end = located[i + 1]["start_offset"]
        else:
            end = len(source)
            fin = source.find("FIN DEL TOMO I", start)
            if fin > start:
                end = fin + len("FIN DEL TOMO I.")
        body = source[start:end] if start >= 0 else ""
        records.append(
            {
                "n": i + 1,
                "heading": sec["heading"],
                "start_offset": start,
                "end_offset": end,
                "first_anchor": sec["first_anchor"],
                "last_anchor": last_n_words(body, 8),
            }
        )
    return records


def cross_check_map(
    source: str, records: list[dict[str, Any]], *, require_count_match: bool = True
) -> list[str]:
    problems: list[str] = []
    regex_count = regex_header_count(source)
    ai_count = len(records)

    if require_count_match and ai_count != regex_count:
        problems.append(
            f"Section count mismatch: AI map has {ai_count} sections, "
            f"regex header count is {regex_count}."
        )

    for rec in records:
        if rec["start_offset"] < 0:
            problems.append(
                f"Section {rec['n']} ({rec['heading']!r}): anchor NOT found in "
                f"source: {rec['first_anchor']!r}"
            )
        else:
            anchor = rec["first_anchor"]
            at = source[rec["start_offset"] : rec["start_offset"] + len(anchor)]
            if at != anchor:
                problems.append(
                    f"Section {rec['n']} ({rec['heading']!r}): anchor offset mismatch "
                    f"at {rec['start_offset']}"
                )

    # offsets should be strictly increasing for found anchors
    offsets = [r["start_offset"] for r in records if r["start_offset"] >= 0]
    if offsets != sorted(offsets):
        problems.append("Section offsets are not in ascending order after sorting.")

    return problems


def write_problems(path: str, problems: list[str]) -> None:
    lines = ["MAP CROSS-CHECK FAILED", f"Time: {datetime.now(timezone.utc).isoformat()}", ""]
    lines.extend(f"- {p}" for p in problems)
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def collect_sections_from_regex(source: str) -> list[tuple[str, str]]:
    """
    Deterministic section list from line-start headers (Bernal Díaz CAPITULO lines).
    Also picks up Bernal's preamble (OTANDO estado...) before Capítulo I.
    """
    sections: list[tuple[str, str]] = []

    header_hits = list(REGEX_SECTION_HEADER.finditer(source))
    if not header_hits:
        return sections

    first_header = header_hits[0].start()
    preamble = REGEX_PREAMBLE_START.search(source, 0, first_header)
    if preamble and preamble.start() < first_header:
        sections.append(("PREÁMBULO", first_n_words(source[preamble.start() :], 8)))

    seen_starts: set[int] = set()
    for m in header_hits:
        pos = m.start()
        if pos in seen_starts:
            continue
        seen_starts.add(pos)
        heading = m.group(1).strip()
        if len(heading) > 120:
            heading = heading[:117] + "..."
        anchor = first_n_words(source[pos:], 8)
        sections.append((heading, anchor))

    return sections


def build_map(source_path: str, map_path: str, problems_path: str) -> int:
    if not os.path.isfile(source_path):
        print(f"ERROR: SOURCE_FILE not found: {source_path}", file=sys.stderr)
        return 1

    print(f"Reading source: {source_path}")
    source = read_text(source_path)
    source_hash = sha256_file(source_path)

    if MAP_BUILD_MODE == "regex":
        print("Building section map from regex headers (CAPITULO / PREÁMBULO)...")
        raw_sections = collect_sections_from_regex(source)
    else:
        print("Running AI boundary pass (one cursor-agent call per chunk)...")
        raw_sections = collect_sections_from_ai(source)

    if not raw_sections:
        print("ERROR: No sections detected.", file=sys.stderr)
        return 1

    located = locate_sections(source, raw_sections)
    records = finalize_section_records(source, located)
    problems = cross_check_map(
        source, records, require_count_match=(MAP_BUILD_MODE != "regex")
    )

    if problems:
        write_problems(problems_path, problems)
        print(f"STOPPED — map cross-check failed. See {problems_path}", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    payload = {
        "source_filename": os.path.basename(source_path),
        "source_sha256": source_hash,
        "section_count": len(records),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sections": records,
    }
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print_map_summary(records, regex_header_count(source))
    print(f"\nMap written: {map_path}")
    return 0


def safe_print(text: str) -> None:
    """Print without crashing on Windows cp1252 consoles."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def print_map_summary(records: list[dict[str, Any]], regex_count: int | None = None) -> None:
    print(f"\nSection map ({len(records)} sections):")
    for rec in records:
        preview = rec["first_anchor"]
        words = preview.split()
        if len(words) > 8:
            preview = " ".join(words[:8])
        safe_print(f"  {rec['n']:3d}. {rec['heading']}  —  {preview}")
    if regex_count is not None:
        safe_print(f"\nRegex header count: {regex_count}  |  AI section count: {len(records)}")


def load_map(map_path: str) -> dict[str, Any]:
    with open(map_path, encoding="utf-8") as f:
        return json.load(f)


def print_existing_map(map_path: str) -> int:
    if not os.path.isfile(map_path):
        print(f"ERROR: MAP_FILE not found: {map_path}", file=sys.stderr)
        return 1
    data = load_map(map_path)
    records = data.get("sections", [])
    print_map_summary(records, regex_header_count(read_text(SOURCE_FILE)) if os.path.isfile(SOURCE_FILE) else None)
    print(f"\nMap file: {map_path}")
    print(f"Source: {data.get('source_filename')}  sha256: {data.get('source_sha256')}")
    print(f"Built: {data.get('built_at')}")
    return 0


# ---------------------------------------------------------------------------
# Translation run
# ---------------------------------------------------------------------------

TRANSLATE_INSTRUCTION = (
    "Translate ONLY the source text below. Do not load or reference any other section. "
    "Output only <english>, <spanish>, and <flags> blocks."
)


# ---------------------------------------------------------------------------
# Post-translation QA (deterministic — do not trust model self-report in flags)
# ---------------------------------------------------------------------------


def strip_apparatus(text: str) -> tuple[str, bool]:
    """Remove known edition/page markers. Returns (cleaned_text, was_stripped)."""
    cleaned = text
    stripped = False
    for pattern in APPARATUS_PATTERNS:
        if pattern.search(cleaned):
            stripped = True
            cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned.strip(), stripped


def looks_truncated(text: str, *, require_sentence_end: bool = True) -> bool:
    """True when prose ends mid-thought (unclosed paren, no sentence end, etc.)."""
    t = text.strip()
    if not t:
        return True
    if TRUNCATION_END_RE.search(t):
        return True
    if t.endswith("(") or t.endswith("[") or t.endswith("{"):
        return True
    if require_sentence_end and not SENTENCE_END_RE.search(t):
        return True
    return False


def contains_apparatus(text: str) -> bool:
    return any(p.search(text) for p in APPARATUS_PATTERNS)


def build_name_hints(source_chunk: str) -> str:
    """
    Inject critical name anchors from source OCR into the prompt.
    Bernal-specific: xpoual = Cristóbal; do not conflate with Gonzalo de Sandoval.
    """
    hints: list[str] = []
    lower = source_chunk.lower()

    if SOURCE_XPOUAL_OLID_RE.search(source_chunk):
        hints.append(
            "CRITICAL NAME: source xpoual de oli = Cristóbal de Olid. "
            "Use Cristóbal de Olid in both languages. Never write Gonzalo de Olid."
        )
    if re.search(r"gonzalo de sandoval", lower):
        hints.append(
            "Gonzalo de Sandoval is a different person from Cristóbal de Olid — keep names distinct."
        )
    if re.search(r"ger[oó]nimo de aguilar", lower):
        hints.append("Gerónimo de Aguilar — preserve this name exactly.")

    if not hints:
        return ""
    return "NAME ANCHORS (mandatory):\n" + "\n".join(f"- {h}" for h in hints)


def validate_name_entities(source_chunk: str, english: str, spanish: str) -> list[str]:
    problems: list[str] = []
    combined = f"{english}\n{spanish}"

    if SOURCE_XPOUAL_OLID_RE.search(source_chunk):
        if GONZALO_OLID_RE.search(combined):
            problems.append(
                "name error: Gonzalo de Olid in output but source has xpoual de oli (Cristóbal de Olid)"
            )
        if not CRISTOBAL_OLID_RE.search(combined) and " de olid" in source_chunk.lower():
            problems.append(
                "name error: Cristóbal de Olid expected (source xpoual de oli) but not found in output"
            )

    return problems


def strip_identity_mappings_from_flags(flags: str) -> tuple[str, int]:
    """Remove no-op entries like hato→hato from flags; return (cleaned, removed_count)."""
    removed = 0

    def _drop(match: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        return ""

    cleaned = IDENTITY_FLAG_RE.sub(_drop, flags)
    cleaned = re.sub(r";\s*;", ";", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(), removed


def validate_flags_claims(
    flags: str, source_chunk: str, english: str, spanish: str
) -> list[str]:
    """Reject flags that rationalize factual errors (content checks are separate)."""
    problems: list[str] = []
    if not flags.strip():
        return problems

    if FALSE_OLID_FLAG_RE.search(flags) and SOURCE_XPOUAL_OLID_RE.search(source_chunk):
        problems.append(
            "flags falsely claim xpoual→Gonzalo; source xpoual de oli is Cristóbal de Olid"
        )

    return problems


def source_ends_cleanly(source_chunk: str) -> bool:
    """False when the source slice ends mid-sentence (OCR / section boundary)."""
    t = source_chunk.strip()
    if not t:
        return False
    if TRUNCATION_END_RE.search(t):
        return False
    if t.endswith("(") or t.endswith("[") or t.endswith("{"):
        return False
    return bool(SENTENCE_END_RE.search(t))


def effective_source_words(source_chunk: str) -> int:
    """Word count for coverage checks, excluding edition back matter."""
    cut = source_chunk
    for marker in (
        "FIN DEL TOMO I",
        "Publicaciones de Genaro García",
        "Publicaciones de Genaro Garcia",
        "Precio de\nsubscripci",
    ):
        idx = cut.find(marker)
        if idx > len(cut) * 0.4:
            cut = cut[:idx]
    cut, _ = strip_apparatus(cut)
    cut = re.sub(r"\[Skipped:[^\]]+\]", " ", cut)
    return word_count(cut)


def split_at_paragraph(text: str) -> tuple[str, str]:
    """Split source text near the middle on a paragraph boundary."""
    if not text.strip():
        return text, ""
    mid = len(text) // 2
    split_at = text.rfind("\n\n", 0, mid + 800)
    if split_at < len(text) * 0.2:
        split_at = text.find("\n\n", mid)
    if split_at < 0:
        split_at = mid
    return text[:split_at].strip(), text[split_at:].strip()


def validate_section_output(
    english: str,
    spanish: str,
    flags: str,
    source_chunk: str,
) -> list[str]:
    """Return a list of validation failures; empty means OK to write."""
    problems: list[str] = []

    if not english.strip():
        problems.append("empty <english> block")
    if not spanish.strip():
        problems.append("empty <spanish> block")
    if not flags.strip():
        problems.append("empty <flags> block")

    if english.strip() and looks_truncated(
        english, require_sentence_end=source_ends_cleanly(source_chunk)
    ):
        problems.append("english appears truncated (incomplete ending)")
    if spanish.strip() and looks_truncated(spanish, require_sentence_end=False):
        problems.append("spanish appears truncated (incomplete ending)")

    es_words = word_count(spanish)
    en_words = word_count(english)
    src_words = effective_source_words(source_chunk)

    if es_words > 0:
        ratio = en_words / es_words
        if ratio < RATIO_THRESHOLD:
            problems.append(
                f"en/es word ratio too low ({ratio:.2f}; minimum {RATIO_THRESHOLD})"
            )
        if ratio > RATIO_MAX:
            problems.append(
                f"en/es word ratio too high ({ratio:.2f}; maximum {RATIO_MAX})"
            )

    if src_words > 80 and en_words > 0:
        src_ratio = en_words / src_words
        if src_ratio < SOURCE_EN_RATIO_MIN:
            problems.append(
                f"english too short vs source ({en_words}/{src_words} words, "
                f"ratio {src_ratio:.2f}; minimum {SOURCE_EN_RATIO_MIN})"
            )

    for label, block in (("english", english), ("spanish", spanish)):
        if contains_apparatus(block):
            problems.append(f"{label} contains edition apparatus (folio/page marker)")

    problems.extend(validate_name_entities(source_chunk, english, spanish))
    problems.extend(validate_flags_claims(flags, source_chunk, english, spanish))
    return problems


def parse_output_sections(output_path: str) -> dict[int, dict[str, str]]:
    """Parse OUTPUT_FILE into full section blocks including english/spanish text."""
    if not os.path.isfile(output_path):
        return {}
    text = read_text(output_path)
    result: dict[int, dict[str, str]] = {}
    for m in OUTPUT_SECTION_PARSE_RE.finditer(text):
        n = int(m.group("n"))
        flags = m.group("flags").strip()
        result[n] = {
            "heading": m.group("heading").strip(),
            "english": m.group("english").strip(),
            "spanish": m.group("spanish").strip(),
            "flags": flags,
            "voice": extract_voice_line(flags),
        }
    return result


def remove_sections_from_output(output_path: str, section_nums: set[int]) -> None:
    """Remove section blocks from output file (for --force re-runs)."""
    if not os.path.isfile(output_path):
        return
    text = read_text(output_path)

    def _drop(m: re.Match[str]) -> str:
        return "" if int(m.group("n")) in section_nums else m.group(0)

    new_text = OUTPUT_SECTION_PARSE_RE.sub(_drop, text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
    if new_text:
        new_text += "\n\n"
    write_text(output_path, new_text)


def remove_sections_from_voice_log(voice_log_path: str, section_nums: set[int]) -> None:
    if not os.path.isfile(voice_log_path):
        return
    prefix = tuple(f"Section {n} |" for n in section_nums)
    kept = [
        line
        for line in read_text(voice_log_path).splitlines()
        if line and not line.startswith(prefix)
    ]
    write_text(voice_log_path, ("\n".join(kept) + "\n") if kept else "")


def old_section_to_new(old_n: int) -> int | None:
    """
    Map section numbers from the 142-section map (with false body-text headers
    at old 98 and 131) to the corrected 140-section map.
    """
    if old_n in (98, 131):
        return None
    if old_n < 98:
        return old_n
    if old_n < 131:
        return old_n - 1
    return old_n - 2


def migrate_output_142_to_140(output_path: str, backup_path: str) -> int:
    """
    Renumber translation_output.txt after section_map fix (142 → 140 sections).
    Drops false-positive sections 98 and 131; renumbers the rest.
    """
    if not os.path.isfile(output_path):
        print(f"ERROR: output not found: {output_path}", file=sys.stderr)
        return 1

    text = read_text(output_path)
    write_text(backup_path, text)

    blocks: dict[int, str] = {}
    for m in OUTPUT_SECTION_PARSE_RE.finditer(text):
        n = int(m.group("n"))
        blocks[n] = m.group(0).strip() + "\n\n"

    if not blocks:
        print("ERROR: no sections found to migrate.", file=sys.stderr)
        return 1

    dropped = [n for n in sorted(blocks) if old_section_to_new(n) is None]
    migrated: list[tuple[int, str]] = []
    for old_n in sorted(blocks):
        new_n = old_section_to_new(old_n)
        if new_n is None:
            continue
        block = blocks[old_n]
        block = re.sub(
            r"=== (.+?) — Section \d+ ===",
            lambda m, nn=new_n: f"=== {m.group(1)} — Section {nn} ===",
            block,
            count=1,
        )
        migrated.append((new_n, block))

    out_text = "".join(block for _, block in migrated)
    write_text(output_path, out_text)

    # Migrate voice log if present
    if os.path.isfile(VOICE_LOG_FILE):
        new_lines: list[str] = []
        for line in read_text(VOICE_LOG_FILE).splitlines():
            if not line.startswith("Section "):
                continue
            parts = line.split(" | ", 2)
            if len(parts) < 3:
                continue
            old_n = int(parts[0].replace("Section ", "").strip())
            new_n = old_section_to_new(old_n)
            if new_n is None:
                continue
            new_lines.append(f"Section {new_n} | {parts[1]} | {parts[2]}")
        write_text(VOICE_LOG_FILE, ("\n".join(new_lines) + "\n") if new_lines else "")

    print(f"Migrated {len(blocks)} → {len(migrated)} sections (dropped {dropped})")
    print(f"Backup: {backup_path}")
    return 0


def validate_existing_output(
    source_path: str, map_path: str, output_path: str
) -> int:
    """Audit translation_output.txt with the same gates used during translation."""
    if not os.path.isfile(output_path):
        print(f"ERROR: output not found: {output_path}", file=sys.stderr)
        return 1
    if not os.path.isfile(map_path):
        print(f"ERROR: map not found: {map_path}", file=sys.stderr)
        return 1

    source = read_text(source_path)
    map_data = load_map(map_path)
    sections_by_n = {s["n"]: s for s in map_data.get("sections", [])}
    parsed = parse_output_sections(output_path)

    if not parsed:
        print("No sections found in output.")
        return 1

    failed = 0
    print(f"Validating {len(parsed)} sections in {output_path}\n")
    for n in sorted(parsed):
        sec = parsed[n]
        map_sec = sections_by_n.get(n)
        if not map_sec:
            safe_print(f"  Section {n}: WARNING — not in section map")
            continue
        chunk = source[map_sec["start_offset"] : map_sec["end_offset"]]
        problems = validate_section_output(
            sec["english"], sec["spanish"], sec["flags"], chunk
        )
        if problems:
            failed += 1
            safe_print(f"  Section {n} FAIL — {sec['heading'][:60]}")
            for p in problems:
                safe_print(f"    - {p}")
        else:
            safe_print(f"  Section {n} OK")

    print(f"\n--- {failed} section(s) failed validation ---")
    return 1 if failed else 0


def completed_section_numbers(output_path: str) -> set[int]:
    if not os.path.isfile(output_path):
        return set()
    text = read_text(output_path)
    return {int(m.group(1)) for m in OUTPUT_SECTION_RE.finditer(text)}


def strip_code_fences(text: str) -> str:
    return re.sub(r"```[\w]*\n?", "", text).replace("```", "")


def extract_tag(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL | re.IGNORECASE)
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def extract_voice_line(flags: str) -> str:
    """Return the first 'voice: …' line from a <flags> block (UTP requires it first)."""
    for line in flags.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = VOICE_LINE_RE.match(stripped)
        if m:
            return f"voice: {m.group(1).strip()}"
        if stripped.lower().startswith("voice:"):
            return stripped
    return ""


def parse_completed_sections(output_path: str) -> dict[int, dict[str, str]]:
    """Parse OUTPUT_FILE into {section_n: {heading, voice, flags}} for resume / voice carry."""
    if not os.path.isfile(output_path):
        return {}
    text = read_text(output_path)
    result: dict[int, dict[str, str]] = {}
    for m in OUTPUT_SECTION_BLOCK_RE.finditer(text):
        n = int(m.group("n"))
        flags = m.group("flags").strip()
        result[n] = {
            "heading": m.group("heading").strip(),
            "flags": flags,
            "voice": extract_voice_line(flags),
        }
    return result


def append_voice_log(section_n: int, heading: str, voice_line: str) -> None:
    append_text(VOICE_LOG_FILE, f"Section {section_n} | {heading} | {voice_line}\n")


def previous_voice_for_section(
    section_n: int, completed: dict[int, dict[str, str]]
) -> str | None:
    """
    Voice carry rule: inject only when the immediately preceding section (n-1)
    completed successfully (present in OUTPUT_FILE). Skip after a skip.
    """
    if section_n <= 1:
        return None
    prev = completed.get(section_n - 1)
    if not prev:
        return None
    voice = prev.get("voice", "")
    return voice or None


def build_source_prompt_block(section_n: int, chunk: str, completed: dict[int, dict[str, str]]) -> str:
    """Source text block with optional PREVIOUS VOICE and NAME ANCHORS."""
    parts: list[str] = []
    prev_voice = previous_voice_for_section(section_n, completed)
    if prev_voice:
        parts.append(f"PREVIOUS VOICE: {prev_voice}")
    name_hints = build_name_hints(chunk)
    if name_hints:
        parts.append(name_hints)
    parts.append(chunk)
    return "\n\n".join(parts)


def print_voice_summary(output_path: str) -> None:
    """Report distinct voices and section numbers where the voice label changed."""
    completed = parse_completed_sections(output_path)
    if not completed:
        return

    ordered = sorted(completed.items())
    voice_lines = [info.get("voice", "") for _, info in ordered if info.get("voice")]
    if not voice_lines:
        print("Voice: no voice lines found in completed sections.")
        return

    distinct = set(voice_lines)
    safe_print(f"\nVoice summary: {len(distinct)} distinct voice(s) across {len(voice_lines)} logged section(s)")

    prev_voice: str | None = None
    for n, info in ordered:
        voice = info.get("voice", "")
        if not voice:
            continue
        if prev_voice is None:
            safe_print(f"  Section {n} ({info['heading']}): initial — {voice}")
        elif voice != prev_voice:
            safe_print(
                f"  Section {n} ({info['heading']}): voice changed — "
                f"{prev_voice}  →  {voice}"
            )
        prev_voice = voice

    safe_print(f"Voice log: {VOICE_LOG_FILE}")


def is_blocked_output(text: str) -> bool:
    cleaned = strip_code_fences(text).strip()
    if len(cleaned) < 80:
        return True
    if not extract_tag(cleaned, "english"):
        lower = cleaned.lower()
        if any(marker in lower for marker in BLOCK_MARKERS):
            return True
        # no english block at all → treat as failure
        if "<english>" not in lower:
            return True
    return False


def append_skip(skip_path: str, section_n: int, chunk: str, reason: str = "") -> None:
    header = f"<<<BLOCKED id={section_n}>>>"
    if reason:
        header += f" reason={reason}"
    append_text(skip_path, f"{header}\n{chunk}\n<<<END id={section_n}>>>\n\n")


def append_validation_failure(section_n: int, problems: list[str]) -> None:
    lines = [f"{section_n}:"] + [f"  - {p}" for p in problems] + [""]
    append_text(VALIDATION_FAILED_FILE, "\n".join(lines))


def finalize_translation_output(
    english: str,
    spanish: str,
    flags: str,
    chunk: str,
) -> tuple[str, str, str]:
    """Strip apparatus, clean flags, append runner QA notes."""
    english, en_stripped = strip_apparatus(english)
    spanish, es_stripped = strip_apparatus(spanish)
    flags, pad_removed = strip_identity_mappings_from_flags(flags)
    qa_notes: list[str] = []
    if en_stripped or es_stripped:
        qa_notes.append("edition apparatus stripped by runner (folio/page marker)")
    if pad_removed:
        qa_notes.append(f"removed {pad_removed} no-op flag mapping(s) (e.g. hato→hato)")
    if qa_notes:
        flags = flags + "\nqa: " + "; ".join(qa_notes) + "."
    return english, spanish, flags


def call_and_extract(prompt: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Run cursor-agent; return (english, spanish, flags, error_message)."""
    out, err = call_cursor_agent(prompt)
    if err or is_blocked_output(out):
        return None, None, None, err or "blocked/refusal"
    cleaned = strip_code_fences(out)
    return (
        extract_tag(cleaned, "english"),
        extract_tag(cleaned, "spanish"),
        extract_tag(cleaned, "flags"),
        None,
    )


def translate_one_section(
    *,
    n: int,
    heading: str,
    chunk: str,
    utp: str,
    completed_sections: dict[int, dict[str, str]],
    total: int,
) -> tuple[bool, str, dict[str, str] | None]:
    """
    Translate a single section with retries. Returns (success, status, section_info).
    Oversized sections are split into two sequential calls.
    """
    parts: list[tuple[str, str]] = []
    src_words = word_count(chunk)
    if src_words > MAX_SECTION_WORDS:
        p1, p2 = split_at_paragraph(chunk)
        if p1 and p2:
            parts = [("PART 1 of 2", p1), ("PART 2 of 2", p2)]
            safe_print(f"  Section {n}: splitting ({src_words} words → 2 parts)")

    if not parts:
        parts = [("COMPLETE", chunk)]

    retry_note = ""
    for attempt in range(1 + MAX_RETRIES):
        english_parts: list[str] = []
        spanish_parts: list[str] = []
        flags = ""
        failed_err: str | None = None

        for part_label, part_text in parts:
            source_block = build_source_prompt_block(n, part_text, completed_sections)
            part_instruction = TRANSLATE_INSTRUCTION
            if len(parts) > 1:
                part_instruction += (
                    f"\n\nThis is {part_label} of one section — translate ONLY this excerpt. "
                    "Output complete tagged blocks for this part only."
                )
            prompt = f"{utp}\n\n{part_instruction}\n\n{source_block}{retry_note}"
            en, es, fl, err = call_and_extract(prompt)
            if err:
                failed_err = err
                break
            english_parts.append(en or "")
            spanish_parts.append(es or "")
            if fl:
                flags = fl

        if failed_err:
            if attempt < MAX_RETRIES:
                retry_note = (
                    f"\n\nPREVIOUS ATTEMPT FAILED ({failed_err}). "
                    "Output complete <english>, <spanish>, and <flags> blocks."
                )
                continue
            append_skip(SKIP_FILE, n, chunk, failed_err)
            return False, f"SKIPPED ({failed_err})", None

        english = "\n\n".join(p for p in english_parts if p.strip())
        spanish = "\n\n".join(p for p in spanish_parts if p.strip())
        english, spanish, flags = finalize_translation_output(
            english, spanish, flags or "", chunk
        )

        problems = validate_section_output(english, spanish, flags, chunk)
        if problems:
            if attempt < MAX_RETRIES:
                retry_note = (
                    "\n\nPREVIOUS ATTEMPT REJECTED BY QA — fix ALL of these:\n"
                    + "\n".join(f"- {p}" for p in problems)
                )
                safe_print(f"  Section {n}: validation failed (attempt {attempt + 1}), retrying...")
                for p in problems:
                    safe_print(f"    - {p}")
                continue

            append_skip(SKIP_FILE, n, chunk, "validation_failed")
            append_validation_failure(n, problems)
            return False, "VALIDATION FAILED", None

        voice_line = extract_voice_line(flags)
        es_words = word_count(spanish)
        en_words = word_count(english)
        ratio = (en_words / es_words) if es_words else 0.0
        status = "ok"
        if len(parts) > 1:
            status = f"ok (split {len(parts)} parts, ratio {ratio:.2f})"
        elif es_words and ratio < RATIO_THRESHOLD + 0.05:
            status = f"ok (ratio {ratio:.2f})"

        block = (
            f"=== {heading} — Section {n} ===\n"
            f"<english>{english}</english>\n"
            f"<spanish>{spanish}</spanish>\n"
            f"<flags>{flags}</flags>\n\n"
        )
        append_text(OUTPUT_FILE, block)
        append_voice_log(n, heading, voice_line)
        info = {"heading": heading, "flags": flags, "voice": voice_line}
        return True, status, info

    return False, "SKIPPED", None


def translate_sections(
    source_path: str,
    map_path: str,
    limit: int | None,
    section_filter: set[int] | None,
    force: bool,
) -> int:
    if not os.path.isfile(source_path):
        print(f"ERROR: SOURCE_FILE not found: {source_path}", file=sys.stderr)
        return 1
    if not os.path.isfile(map_path):
        print(f"ERROR: MAP_FILE not found: {map_path} — run with --build-map first.", file=sys.stderr)
        return 1
    if not os.path.isfile(UTP_FILE):
        print(f"ERROR: UTP_FILE not found: {UTP_FILE}", file=sys.stderr)
        return 1

    source = read_text(source_path)
    current_hash = sha256_file(source_path)
    map_data = load_map(map_path)
    stored_hash = map_data.get("source_sha256", "")

    if current_hash != stored_hash:
        print(
            "REFUSED: SOURCE_FILE sha256 does not match the map.\n"
            f"  map has:  {stored_hash}\n"
            f"  file has: {current_hash}\n"
            "The source changed — offsets are stale. Rebuild with --build-map.",
            file=sys.stderr,
        )
        return 1

    utp = read_text(UTP_FILE)
    sections: list[dict[str, Any]] = map_data.get("sections", [])
    total = len(sections)
    done = completed_section_numbers(OUTPUT_FILE)

    if section_filter:
        pending = [s for s in sections if s["n"] in section_filter]
        missing = section_filter - {s["n"] for s in pending}
        if missing:
            print(f"WARNING: section(s) not in map: {sorted(missing)}", file=sys.stderr)
        if force and pending:
            nums = {s["n"] for s in pending}
            print(f"Force re-run: removing section(s) {sorted(nums)} from output")
            remove_sections_from_output(OUTPUT_FILE, nums)
            remove_sections_from_voice_log(VOICE_LOG_FILE, nums)
            done -= nums
        elif not force:
            already = [s["n"] for s in pending if s["n"] in done]
            if already:
                print(
                    f"Section(s) {already} already in output — use --force to re-translate.",
                    file=sys.stderr,
                )
            pending = [s for s in pending if s["n"] not in done]
    else:
        pending = [s for s in sections if s["n"] not in done]

    if limit is not None:
        pending = pending[:limit]

    # Fresh sidecar logs for this run
    write_text(LOWRATIO_FILE, "")
    write_text(VALIDATION_FAILED_FILE, "")

    print(f"Map: {total} sections | already done: {len(done)} | to process: {len(pending)}")

    # Resume-safe voice carry: last completed section's voice comes from OUTPUT_FILE
    completed_sections = parse_completed_sections(OUTPUT_FILE)
    if completed_sections:
        last_n = max(completed_sections)
        last_voice = completed_sections[last_n].get("voice", "")
        if last_voice:
            safe_print(f"Resume voice carry: last completed Section {last_n} — {last_voice}")

    skipped_nums: list[int] = []
    validation_failed_nums: list[int] = []
    completed = 0

    for sec in pending:
        n = sec["n"]
        heading = sec["heading"]
        start = sec["start_offset"]
        end = sec["end_offset"]
        chunk = source[start:end]

        ok, status, info = translate_one_section(
            n=n,
            heading=heading,
            chunk=chunk,
            utp=utp,
            completed_sections=completed_sections,
            total=total,
        )

        if not ok:
            skipped_nums.append(n)
            if status == "VALIDATION FAILED":
                validation_failed_nums.append(n)
            print(f"Section {n}/{total} — {100 * n / total:.1f}% — {status}", flush=True)
            continue

        completed_sections[n] = info  # type: ignore[assignment]
        completed += 1
        print(f"Section {n}/{total} — {100 * n / total:.1f}% — [{status}]", flush=True)

    print("\n--- Summary ---")
    print(f"Completed this run: {completed}")
    print(f"Skipped: {len(skipped_nums)}" + (f"  (see {SKIP_FILE})" if skipped_nums else ""))
    print(
        f"Validation failed: {len(validation_failed_nums)}"
        + (f"  (see {VALIDATION_FAILED_FILE})" if validation_failed_nums else "")
    )
    print(f"Output: {OUTPUT_FILE}")
    print_voice_summary(OUTPUT_FILE)
    return 1 if validation_failed_nums or skipped_nums else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass

    parser = argparse.ArgumentParser(
        description="Batch literary translation via cursor-agent (fresh context per section)."
    )
    parser.add_argument(
        "--build-map",
        action="store_true",
        help="Run AI boundary pass once and write MAP_FILE (with cross-check tripwire).",
    )
    parser.add_argument(
        "--map-only",
        action="store_true",
        help="Print existing MAP_FILE without calling the AI (requires MAP_FILE).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Translate only the first N pending sections (cheap test run).",
    )
    parser.add_argument(
        "--sections",
        type=str,
        default=None,
        metavar="N,N,...",
        help="Translate only these section numbers (comma-separated). Use with --force to re-run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --sections: remove existing blocks and re-translate those sections.",
    )
    parser.add_argument(
        "--validate-output",
        action="store_true",
        help="Audit translation_output.txt with QA gates (no cursor-agent calls).",
    )
    parser.add_argument(
        "--migrate-output",
        action="store_true",
        help="Renumber output from old 142-section map to corrected 140-section map.",
    )
    args = parser.parse_args()

    section_filter: set[int] | None = None
    if args.sections:
        section_filter = {int(x.strip()) for x in args.sections.split(",") if x.strip()}

    if args.map_only:
        return print_existing_map(MAP_FILE)
    if args.build_map:
        return build_map(SOURCE_FILE, MAP_FILE, PROBLEMS_FILE)
    if args.validate_output:
        return validate_existing_output(SOURCE_FILE, MAP_FILE, OUTPUT_FILE)
    if args.migrate_output:
        backup = os.path.join(PROJECT_DIR, "translation_output_v142_backup.txt")
        return migrate_output_142_to_140(OUTPUT_FILE, backup)
    return translate_sections(
        SOURCE_FILE, MAP_FILE, args.limit, section_filter, args.force
    )


if __name__ == "__main__":
    sys.exit(main())
