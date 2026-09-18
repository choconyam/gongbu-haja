# gongbu-haja — study notes from lecture handouts and recordings

> **한국어**: [`README.md`](README.md)

A Korean-language study-note workflow that turns lecture handouts and recordings into traceable notes. New notes are produced by the model and reasoning level already selected in the user's current session; deterministic extraction, transcription, build, and structural checks stay local. Optional managed runs retain role-separated agents and reproducible gates.

All prompts, rules, CLI output and the detailed documentation under [`docs/`](docs/) are written in Korean and target Korean university lectures.

## Two note modes

| Mode | Output |
|---|---|
| **faithful** (자료 충실형) | Only the handout and the verified instructor explanation, organized for review. Markdown by default. |
| **deep** (심화 이해형) | Adds verified background, intermediate reasoning and derivations under each original slide. Print-ready PDF by default. |

The mode selects how far the explanation goes, not which model is used.

## Quick start

```bash
git clone https://github.com/choconyam/gongbu-haja
cd gongbu-haja
```

Put a handout and a recording (or an existing transcript) under `input/<lecture>/`, open the folder with Codex, Claude Code or Cursor, and ask for a study note. Recordings are transcribed locally with Whisper and are not uploaded to a transcription service.

## License

MIT — see [`LICENSE`](LICENSE).
