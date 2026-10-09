# Calendar, projects, home automation, and choosing a role

## Decide what your assistant is for

Before adding features, decide on the job. It changes her instructions, what she remembers, and what you connect.

| Role | Change in `Aurora.py` and your files | Connect |
| --- | --- | --- |
| Administrative assistant | Short replies, confirm before acting, read the day's schedule at launch | Calendar, task list, email summaries |
| Study or teaching aid | Use the `lecture` mode; put your courses and terms in `config/profile.md` | Course notes as files she reads at launch |
| Technical helper | Use the `tech_support` mode; list your hardware and software in the profile | Logs or status pages |
| Companion | Warmer tone, more open conversation, a richer memory file | Usually nothing beyond memory |

For a companion, keep two things in mind. Everything you say is sent to the voice provider, so keep private details out of the memory file you would not want stored by a third party. And an assistant tuned to agree with you is pleasant and less useful; the default instructions ask her to say when something is uncertain, and that is worth keeping.

## Google Calendar

Aion reads the next day and a half of your primary Google Calendar at launch, and adds, edits, and undoes events by voice.

1. Go to https://console.cloud.google.com/ and create a project.
2. Under **APIs & Services > Library**, enable the **Google Calendar API**.
3. Under **APIs & Services > OAuth consent screen**, choose External, fill in the app name and your email, and add your own Google account as a test user.
4. Under **APIs & Services > Credentials**, choose **Create credentials > OAuth client ID > Desktop app**. Download the JSON file.
5. Save it as `keys/google_credentials.json` in this project.
6. In `.env`, set `AURORA_NO_CALENDAR=` (empty) and start Aion. A browser window asks you to approve access. After that, `keys/google_token.json` is saved and later launches are silent.

Google's console labels move around; the official walkthrough is at https://developers.google.com/workspace/calendar/api/quickstart/python .

The scope Aion asks for (`calendar.events`) lets it read, create, change, and delete events. Undo after an add deletes the event it just created. Both key files are gitignored; never commit them.

## Tying her into your projects

Three places carry context, from simplest to most work:

1. **`config/profile.md`**: standing facts about you and your work. Read at every launch.
2. **`memory/aion_memory.md`**: notes that change over time. You can edit it in any text editor, and "Aion, remember that ..." appends to it.
3. **New voice commands in code.** `handle_user_commands` in `Aurora.py` checks each finished sentence against regular expressions and runs Python when one matches. The calendar commands are the worked example. To add your own:
   - Add a regex check for the phrase, for example `r"\bproject status\b"`.
   - Call a function that does the work (read a file, call an API) and returns a sentence.
   - Speak the result with `await say(ws, reply)`, and add one line to `CORE_INSTRUCTIONS` telling her the program answers that request, so she stays quiet and lets the result come.

A sketch of a status command that reads a file you keep updated:

```python
    if final and re.search(r"\bproject status\b", text):
        state.handled_utterance = True
        status = (ROOT / "memory" / "project_status.md").read_text(encoding="utf-8")[:1500]
        await add_instructions(ws, "Current project status\n" + status)
        await say(ws, "Summarize the current project status in two sentences.")
        return True
```

## Home automation

[Home Assistant](https://www.home-assistant.io/) exposes a REST API that a voice command can call. Create a long-lived access token in your Home Assistant profile, store the URL and token in `.env` (for example `HA_URL` and `HA_TOKEN`), and call a service:

```python
import requests

def ha_service(domain: str, service: str, entity_id: str) -> bool:
    r = requests.post(
        f"{os.environ['HA_URL']}/api/services/{domain}/{service}",
        headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}"},
        json={"entity_id": entity_id},
        timeout=10,
    )
    return r.ok

# in handle_user_commands:
#   if final and re.search(r"\bturn on the desk lamp\b", text):
#       ok = await asyncio.to_thread(ha_service, "light", "turn_on", "light.desk_lamp")
#       await say(ws, "Lamp on." if ok else "Home Assistant did not respond.")
```

These snippets are starting points and are not part of the tested code. The Home Assistant REST API reference is at https://developers.home-assistant.io/docs/api/rest/ .

## Where to learn more

- OpenAI Live API and Realtime API docs: https://platform.openai.com/docs
- VMagicMirror manual: https://malaybaku.github.io/VMagicMirror/en/
- VRM specification and tools: https://vrm.dev/en/
- python-sounddevice docs (audio devices, sample rates): https://python-sounddevice.readthedocs.io/
