# ASR engine benchmark (macOS)

Compares BadWords' current transcription engine with two Mac-native options on the same audio:

| engine id | what it is | runs on |
|---|---|---|
| `badwords-large-v3` | **Baseline.** BadWords' own path: faster-whisper through `AudioEngine.run_whisper()` with sound islands, verbatim prompt, default settings | CPU (what BadWords uses on every Mac today) |
| `mlx-large-v3` | Same Whisper large-v3 weights via [mlx-whisper](https://pypi.org/project/mlx-whisper/), same decoding settings and prompt | Apple GPU |
| `mlx-large-v3-turbo` | Whisper large-v3-turbo via mlx-whisper | Apple GPU |
| `apple-speech` | Apple `SpeechTranscriber` (Speech framework, macOS 26) | on-device, system service |
| `apple-dictation` | Apple `DictationTranscriber` (older dictation model) | on-device, system service |

Any `badwords-<model>` works (e.g. `badwords-large-v3-turbo`, `badwords-tiny`) if that model is installed.

## What it reports

`RESULTS.md` (table), `fillers.md` (every filler, with what each engine produced at that moment), `results.json`, per-engine `<engine>.json` (BadWords segment/word contract) and `<engine>.txt` (plain transcript).

- **wall / × realtime**: whole engine process, including model load. A warm-up pass on 15 s runs first so downloads and caches don't count.
- **peak RAM**: engine process (BadWords: including its runner child). MLX also reports peak GPU memory. Apple's engines run inside a macOS service, so their memory can't be measured from here.
- **fillers**: words from BadWords' `DEFAULT_BAD_WORDS` list. BadWords depends on these appearing in the transcript.
- **baseline fillers also found**: of the fillers the baseline transcribed, how many the engine also produced at the same position in the word sequence.
- **word diff vs baseline**: word-level edit distance against the baseline. **The baseline is not ground truth.** A difference can be the other engine being right. Check `fillers.md` and the `.txt` files by ear.
- **start Δ**: for words both engines agree on, how far apart their start times are. This matters for cut precision.

All engines get identical input: the chosen audio stream, run through BadWords' own `normalize_audio()` filter (high-pass + light compressor), 48 kHz mono.

## Setup (once)

```sh
# 1. A BadWords v4 install to use as the baseline (default: ~/Library/Application Support/BadWords-dev/BadWords)
# 2. MLX venv + models (models go to .local/bench-cache, ~4.6 GB)
python3.12 -m venv .local/bench-venv && .local/bench-venv/bin/pip install mlx-whisper
# 3. Apple engine (needs Xcode / CLT with the macOS 26 SDK)
xcrun swiftc -O -parse-as-library tools/asr_benchmark/engines/apple_transcribe.swift \
    -o tools/asr_benchmark/build/apple-transcribe
```

## Run

```sh
python3 tools/asr_benchmark/bench.py "<video or audio file>" --stream 0 --lang en
# options: --start/--duration (seconds), --engines a,b,c, --reference <engine>, --out <dir>
```

Use real recorded speech. Synthetic (TTS) voices don't produce natural fillers.
