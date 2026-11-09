# Fix unfinished sentence endings with VoiceStudio (preview)

When an edit ends a sentence where the speaker originally kept talking, the last words often keep a rising or level pitch, so the sentence sounds unfinished. StoryCraft can find those endings and replace the last one to three words with a version in the speaker's own voice that ends the way a finished sentence does.

The voice work is done by **[VoiceStudio](https://github.com/debpalash/VoiceStudio)**, a free, open-source app you install separately. StoryCraft doesn't include VoiceStudio. It connects to the VoiceStudio app running on your computer.

This is a preview. It runs from the command line, writes its results to a folder for you to listen to, and changes nothing in Resolve or in your StoryCraft project.

## Before you start

- **Get the speaker's permission.** You are generating speech the speaker never recorded. Use this only with their consent, for example through a release that covers voice editing. Some countries and US states have specific laws on synthetic voices. In the EU, synthetic audio that could pass as real must be disclosed.
- **Check the model's licence.** VoiceStudio is free for any use, including commercial use. The voice models it downloads have their own licences:

  | Model in VoiceStudio | Licence (from the model's card) | Paid work? |
  |---|---|---|
  | VoiceStudio default (OmniVoice) | CC-BY-NC: non-commercial | No |
  | CosyVoice 3 | Apache-2.0 | Yes, as far as its card states |
  | IndexTTS2 | Custom terms | Read them first |

  VoiceStudio's Model Manager shows each model's terms. For client work, pick **CosyVoice 3**.
- **Keep patches short.** Replacing one to three words can sound natural. Longer passages start to sound synthetic.
- **Watch for lip sync.** A patch that changes the length of the words is visible when the speaker's face is on screen. StoryCraft stretches the patch to the original length when the difference is small, and the report says when it couldn't.

## 1. Install VoiceStudio

**Mac (Apple Silicon, macOS 13.3 or later)**, with Homebrew:

```sh
brew tap debpalash/tap
brew trust --cask debpalash/tap/voicestudio
brew install --cask debpalash/tap/voicestudio
```

Or download the DMG from the [VoiceStudio releases page](https://github.com/debpalash/VoiceStudio/releases/latest) and drag VoiceStudio into Applications. If macOS says the app "is damaged", see VoiceStudio's [Gatekeeper fix](https://github.com/debpalash/VoiceStudio/blob/main/docs/install/macos.md#gatekeeper-quarantine).

**Windows and Linux:** download the installer from the [releases page](https://github.com/debpalash/VoiceStudio/releases/latest).

**Docker (any system, CPU, slower):**

```sh
docker run -d --name voicestudio -p 127.0.0.1:3900:3900 \
  -v voicestudio-data:/app/omnivoice_data ghcr.io/debpalash/voicestudio:stable
```

## 2. Set up a voice engine

1. Open VoiceStudio.
2. Open the **Model Manager**, install **CosyVoice 3** (or keep the default engine for non-commercial work) and make it the active engine. VoiceStudio asks before it downloads a model. The models are several gigabytes.
3. Leave VoiceStudio open while you work. StoryCraft talks to it at `http://127.0.0.1:3900`. No account or key is needed when both apps run on the same computer.

You don't create a voice in VoiceStudio yourself. StoryCraft sends it a clip of the speaker taken from your timeline each time.

## 3. Check the connection

In Terminal, from the StoryCraft folder (replace `<install>` with your StoryCraft install folder, `~/Library/Application Support/StoryCraft` on a Mac):

```sh
"<install>/venv/bin/python3" tools/voicepatch/voicepatch.py doctor
```

It reports whether VoiceStudio answers, which engine and model are active, the model's licence (with a warning if it's non-commercial) and whether VoiceStudio's invisible watermark is on. Keep the watermark on: it lets anyone check later that the audio is synthetic.

## 4. Find endings that sound unfinished

Open the timeline in StoryCraft once, so its transcript and audio are saved, then:

```sh
"<install>/venv/bin/python3" tools/voicepatch/voicepatch.py detect \
  "<install>/saves/timelines/<project>/<timeline>.json"
```

Each line is one sentence ending: its ID, its time, the pitch movement over the last words in semitones, and whether it sounds unfinished. Questions are skipped.

## 5. Patch and listen

**Automatic (the voice speaks the words):** fix the five worst endings and build a listening page:

```sh
"<install>/venv/bin/python3" tools/voicepatch/voicepatch.py batch \
  "<install>/saves/timelines/<project>/<timeline>.json" --out ~/Desktop/voicepatch
```

Open `~/Desktop/voicepatch/index.html` and compare each original with its patched version. For each ending, VoiceStudio clones the voice from 5 to 14 seconds of nearby speech that ends on a finished sentence, speaks the sentence with a full stop three times, and StoryCraft keeps the take whose ending falls most naturally.

**Your delivery (you say the words):** record yourself saying the words with the intonation you want, for example with QuickTime or your phone, then:

```sh
"<install>/venv/bin/python3" tools/voicepatch/voicepatch.py patch \
  "<install>/saves/timelines/<project>/<timeline>.json" --end 499 --mode convert --take my-take.m4a
```

VoiceStudio turns your recording into the speaker's voice while keeping your timing and intonation. This gives you the most control.

Each ending gets its own folder with `original.wav`, `patched.wav`, the takes, the reference clip and a `report.json` showing what was measured before and after, how much the patch was stretched, and which VoiceStudio engine, model and licence produced it.

## Troubleshooting

| Message | What to do |
|---|---|
| VoiceStudio isn't reachable | Open VoiceStudio (or start the Docker container) and wait until it finishes loading. |
| The active engine can't clone a voice | In VoiceStudio's Model Manager, choose an engine with voice cloning (CosyVoice 3 or the default). |
| The timeline's audio isn't on disk any more | Open the timeline in StoryCraft again so it extracts the audio. |
| Target words not found in the take | The generated take said something else. Run it again, or use `--mode convert` with your own recording. |
| VoiceStudio runs on another computer | Add `--url http://<that computer>:3900` and set `VOICESTUDIO_API_KEY` to the key that VoiceStudio was started with. |

## Credits

Voice cloning and conversion are provided by [VoiceStudio](https://github.com/debpalash/VoiceStudio) by Palash Debnath and contributors, licensed under AGPL-3.0. StoryCraft is not affiliated with VoiceStudio and doesn't include any of its code. The voice models belong to their authors and carry their own licences, which VoiceStudio shows in its Model Manager and StoryCraft repeats in every patch report.
