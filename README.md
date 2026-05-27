# Poof

Spoof game processes to complete Discord quests by creating a lightweight process that mimics a target game's executable.

## Safety

This does **not** modify the Discord client, inject code, hook processes, or tamper with Discord's memory.
It only:<br> Copies `/bin/sleep` to a temporary file with the game's name and runs it (just a standard Linux process and nothing related to discord)

## Table of Contents

* [Requirements](#requirements)
* [Installation](#installation)
* [How it works](#how-it-works)
* [Usage](#usage)
* [Find the Process Names](#find-the-process-names)

## Requirements

- Linux (uses `prctl`, Unix sockets, `/proc`)
- Python 3.8+
- Discord desktop client running

## Installation

```bash
pip install poof
```

## How it works

1. **Fake Process** — Copies `/bin/sleep` to `/tmp/<game_name>` and runs it. Discord's process scanner reads `/proc/<pid>/exe` and sees a process named exactly like the target game, auto-registering it as your activity.<br>
Note : The **process name** is nothing but the target game's executable name which you can get from the Discord-Detectable-Apps repo.

2. **Discord RPC** *(optional)* — Connects to Discord's Unix socket and sends a `SET_ACTIVITY` frame with the game's Application ID (same protocol the official GameSDK uses).

## Usage

```bash
# Process-only (enough for quests):
poof 'genshinimpact.exe'

# With rich presence too:
poof 'genshinimpact.exe' --app-id 1234567890

# Use prctl rename instead of binary copy:
poof 'genshinimpact.exe' --app-id 1234567890 --method prctl

# Just RPC, no fake process:
poof 'genshinimpact.exe' --app-id 1234567890 --no-process
```

## Find the Process Names

- **Process names**: [Discord-Detectable-Apps](https://github.com/LoneDestroyer/Discord-Detectable-Apps/blob/main/detectable_apps.txt)
