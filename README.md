# Poof

Spoof game processes **and** complete Discord quests via the API — all from the command line.

## Safety

This does **not** modify the Discord client, inject code, hook processes, or tamper with Discord's memory.
- Process spoofing copies `/bin/sleep` to a temporary file with the game's name (just a standard Linux process)
- Quest API mode sends HTTP requests to Discord's API (same as any other API client)
- **Quest API mode requires your Discord auth token** -- This will be cached and saved to ~/.config/poof

## Table of Contents

* [Requirements](#requirements)
* [Installation](#installation)
* [How it works](#how-it-works)
* [Usage](#usage)
* [Find the Process Names](#find-the-process-names)

## Requirements

- Linux (uses `prctl`, Unix sockets, `/proc`)
- Python 3.8+
- Discord desktop client running (for process spoofing)
- Discord auth token (for quest API mode)

## Installation

```bash
pip install poof-discord
```

## How it works

1. **Process spoofing** — Copies `/bin/sleep` to `/tmp/<game_name>` and runs it. Discord's process scanner reads `/proc/<pid>/exe` and sees a process named like the target game, auto-registering it as your activity.<br>
Note : The process name is nothing but the target game's executable name which you can get from the Discord-Detectable-Apps repo.

2. **Discord RPC** *(optional)* — Connects to Discord's Unix socket and sends a `SET_ACTIVITY` frame with the game's Application ID.

3. **Quest API** *(new)* — Sends authenticated requests to Discord's quest API:
   - `GET /api/v9/users/@me/quests` — list enrolled quests
   - `POST /api/v9/quests/{id}/heartbeat` — send periodic heartbeats

## Usage

### Fake process

```bash
# Process-only (enough for most quests):
poof 'genshinimpact.exe'
```

### Quest API mode

First, get your Discord auth token:

```bash
#follow the instructions to aquire the token:
poof token
```

```bash
#cache your token
poof token "PASTE YOUR TOKEN HERE"
```

Now list and complete quests:

```bash
#list enrolled quests
poof quests

#complete all active quests
poof complete

#complete a specific quest
poof complete --quest-id <quest_id>
```

## Find the Process Names

Process names: [Discord-Detectable-Apps](https://github.com/LoneDestroyer/Discord-Detectable-Apps/blob/main/detectable_apps.txt) || [The-Reliable-Reddit](https://www.reddit.com/r/DiscordQuests/hot/)
