# Bernal Díaz — Tomo I Translation Pipeline

Batch literary translation of *Historia verdadera de la conquista de la Nueva España* (García 1904 OCR) into paired modern **English + Spanish**, using the Wikowí Universal Translation Protocol (UTP).

Each chapter is translated in a **fresh** `cursor-agent` process — no context bleed between sections.

## Setup

1. Install [Cursor CLI](https://cursor.com/docs/cli/overview):  
   `irm 'https://cursor.com/install?win32=true' | iex`
2. Authenticate: `cursor-agent login`
3. Place the OCR source at `C:\Users\drewc\Downloads\Bernal_Diaz_part_1.txt` (or edit `SOURCE_FILE` in `translate_runner.py`).
4. Python 3.10+

## Commands

```powershell
cd C:\Users\drewc\Projects\translation

# One-time: freeze section boundaries (140 chapters, regex headers)
python translate_runner.py --build-map

# Full run or resume (skips sections already in output)
python translate_runner.py

# Audit output without calling the AI
python translate_runner.py --validate-output

# Re-translate specific sections
python translate_runner.py --sections 30,73,97 --force

# Live progress dashboard (separate terminal)
python progress_server.py
# → http://127.0.0.1:8765/
```

## Files

| File | Purpose |
|------|---------|
| `translate_runner.py` | Orchestrator: map, translate, QA gates, retries |
| `utp.txt` | Wikowí headless translation protocol |
| `section_map.json` | Frozen byte-offset map (140 sections) |
| `translation_output.txt` | Deliverable: en/es/flags per section |
| `voice_log.txt` | Detected voice label per section |
| `progress_server.py` | Browser dashboard |

## QA gates (hard-fail + retry)

- English/Spanish truncation detection
- Bilingual word-count parity (0.70–1.45)
- Source coverage minimum
- Edition apparatus strip (folio signatures, page markers)
- Cristóbal de Olid name check (`xpoual de oli`)
- Rejects false `xpoual→Gonzalo` flag claims

## Source

Public-domain OCR: Bernal Díaz del Castillo, ed. García (1904), Tomo I. Not included in this repo (large file; path configured in runner).
