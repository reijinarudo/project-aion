# Voice model options and cost

Aion uses OpenAI's Live API with the `gpt-live-1` model. It sounds natural and responds fast, and it costs money every minute a session is open. This page explains what kind of model that is, what the alternatives are, and what changes in the code if you switch.

Prices and model names change often. Check each provider's pricing page before you commit, and set a monthly spending limit in your account.

## Two kinds of voice assistant

**Speech-to-speech (what Aion uses).** One model takes your audio in and produces audio out over a live connection. Because no step waits for a full transcript, replies start quickly, you can interrupt mid-sentence, and the voice carries tone, pauses, and laughter. Examples:

- OpenAI Live API (`gpt-live-1`): https://platform.openai.com/docs
- OpenAI Realtime API (`gpt-realtime` family): the older protocol Aion used before September 2026
- Google Gemini Live API: https://ai.google.dev/gemini-api/docs/live

**Cascaded pipeline.** Three separate steps: speech-to-text, a text language model, then text-to-speech. Each step adds delay (often one to a few seconds in total), and interruption needs extra code, but you can mix providers, use cheaper text models, or run everything locally for free.

| Step | Hosted options | Local (free, runs on your PC) |
| --- | --- | --- |
| Speech to text | OpenAI transcription, Deepgram, Google | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Vosk](https://alphacephei.com/vosk/) |
| Language model | OpenAI, Anthropic Claude, Google Gemini | [Ollama](https://ollama.com/), [LM Studio](https://lmstudio.ai/) running an open model |
| Text to speech | [ElevenLabs](https://elevenlabs.io/), OpenAI TTS | [Piper](https://github.com/rhasspy/piper), Kokoro |

**Hosted voice agents.** Services such as [ElevenLabs Agents](https://elevenlabs.io/) run the whole cascade for you and give you a voice, a knowledge base, and tools in a web dashboard. You connect to them through their SDK.

## What you hear in each case

| | Speech-to-speech | Cascaded, hosted | Cascaded, fully local |
| --- | --- | --- | --- |
| Time to first word | shortest | longer | longest on modest hardware |
| Interrupting her | built in | needs voice-activity detection code | needs voice-activity detection code |
| Tone and expressiveness | carried end to end | set by the TTS voice | set by the TTS voice; local voices sound flatter |
| Cost | highest per minute | pay per step; text models are cheap | free after hardware |
| Privacy | audio goes to the provider | audio or text goes to each provider | stays on your PC |
| Hardware | any PC | any PC | a recent GPU helps a lot |

## Where the money goes with Aion as written

- The microphone streams to the Live API for the whole session, including while you are silent, and audio input is billed. Close the session when you are done ("Thank you Aion, that is all for now").
- Her instructions, profile, memory file, and calendar are sent at the start of every session. A long memory file costs more on every launch.
- Calendar commands make one extra call to a small text model (`AURORA_EXTRACT_MODEL`).

## Switching providers

The OpenAI-specific code is in `scriptspy/Aurora.py`, in `session_start_payload`, `say`, `add_instructions`, `sender`, `receiver`, and `run_session`. The audio device handling, playback to VB-CABLE, memory, calendar, and command handling do not depend on the provider.

- **Another speech-to-speech API** (for example Gemini Live): replace the websocket URL, the session start message, and the event names in `receiver`. The audio format may differ (Gemini Live returns 24 kHz audio and expects 16 kHz input); `SAMPLE_RATE` and the resampling in `start_microphone` handle the conversion once set correctly.
- **A cascaded pipeline:** replace `run_session` with a loop that records an utterance, transcribes it, sends the text to your language model, synthesizes the reply, and writes the audio to `playback.write()`. Lip sync keeps working because VMagicMirror only listens to the audio on VB-CABLE.

These changes are not implemented in this repository. They are listed so you know where to start.
