# Results: 2026-10-30, MacBook Pro M1 Max (32-core GPU), macOS 26.6.2

**Clip:** 4 min 38 s (278.4 s) of a real outdoor interview, single speaker on a lav, English. Canon C-series MP4, audio stream 1.
BadWords baseline: v4 (`dev-v4` + fixes), faster-whisper 1.2.1, CPU, default settings, 60 sound islands.
Transcripts are not included (private footage). Only the aggregate numbers are.

| engine | wall (incl. load) | × realtime | speed-up vs BadWords | peak RAM | words | fillers | baseline fillers also found | word diff vs baseline | start Δ median / p90 |
|---|---|---|---|---|---|---|---|---|---|
| **badwords-large-v3** (today) | 440.4 s | 0.6× | — | 4.8 GB | 736 | 7 | (baseline) | — | — |
| **mlx-large-v3** (islands) | 55.9 s | 5.0× | **7.9×** | 3.7 GB (+4.0 GB GPU) | 741 | 7 | **7/7** | **1.6%** | **0 / 20 ms** |
| mlx-large-v3-turbo (islands) | 22.8 s | 12.2× | 19× | 2.0 GB (+2.5 GB GPU) | 745 | 6 | 4/7 | 7.1% | 80 / 200 ms |
| mlx-large-v3-whole (one pass) | 33.0 s | 8.4× | 13× | 3.9 GB (+4.0 GB GPU) | 726 | 1 | 0/7 | 9.2% | 12 / 118 ms |
| apple-speech (SpeechTranscriber) | 6.1 s | 45.4× | **72×** | macOS service | 720 | 7 | 5/7 | 12.8% | 87 / 443 ms |
| apple-dictation (DictationTranscriber) | 23.6 s | 11.8× | 19× | macOS service | 615 | 0 | 0/7 | 31.7% | 133 / 470 ms |

Signed word-start offset vs baseline (matched words):

| engine | p10 | median | p90 | words > 250 ms off |
|---|---|---|---|---|
| mlx-large-v3 | −20 ms | 0 ms | +20 ms | 2% |
| mlx-large-v3-turbo | +40 ms | +80 ms | +180 ms | 7% (consistent late shift) |
| apple-speech | −273 ms | +3 ms | +180 ms | 16% (scatter in both directions) |

## Findings

1. **MLX large-v3 is a drop-in replacement on Apple Silicon.** Same weights, same prompt, same island chunking. The transcript is 98.4% identical, all 7 fillers are kept, word timings match within ±20 ms, and it's **7.9× faster** (7 min 20 s → 56 s).
2. **Chunking is what keeps fillers, not the engine.** Whisper applies `initial_prompt` only to the first 30 s window of a `transcribe()` call when `condition_on_previous_text=False` (openai-whisper, mlx-whisper and faster-whisper behave the same). BadWords transcribes each sound-island cluster separately, so its verbatim prompt is active throughout. The same MLX model in one pass found 1 filler instead of 7. Any new engine has to keep per-cluster prompting.
3. **MLX turbo** is 2.5× faster again. It kept most fillers (it also found 2 the baseline missed and missed 3 the baseline found). Its timings run consistently ~80 ms late, which a fixed offset could correct. Reasonable as a "fast" option.
4. **Apple SpeechTranscriber keeps fillers with no prompt at all** (7 found, 5 at the baseline's positions, plus some the baseline didn't produce). It's the fastest by far (72×) and needs no model download. But its word timings scatter (16% of words > 250 ms from Whisper's), which is a problem for word-accurate cuts. Better suited to a quick draft/preview pass than to final cuts. (Whisper's timestamps aren't ground truth either; that needs a hand-labelled clip.)
5. **Apple DictationTranscriber drops every filler** and differs the most. Not suitable.

## Caveats
- One clip, one speaker, English, clean lav audio. Needs more clips (noisy, multiple speakers, other languages) before deciding defaults.
- The baseline is not ground truth. Differences in words and fillers should be checked by ear (the benchmark writes `fillers.md` with timestamps for that).
- Each engine ran once after a warm-up. Expect a few percent run-to-run variation.
- `DEFAULT_BAD_WORDS` includes "em", so "grow 'em" counts as a filler for every engine.
