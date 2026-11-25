# Run AETHER on your laptop (Windows 11, step by step)

This guide assumes nothing is installed yet. It's written for an HP Victus 15 (RTX 3050 Laptop 4 GB, Ryzen 5 5600H, 16 GB RAM), but works on any Windows 11 PC. Plan on about 30 minutes, most of it downloads.

Commands go in **PowerShell**: press the Windows key, type `powershell`, press Enter. Paste a command with right-click.

## 1. Install three things (one time)

1. **Python 3.11.** Get it from <https://www.python.org/downloads/release/python-3119/>, choosing "Windows installer (64-bit)". In the installer, tick **"Add python.exe to PATH"** and keep **"py launcher"** ticked. It has to be 3.11 (3.12 also works): the fast GPU build of the model runner is published for those versions.
2. **Git.** Get it from <https://git-scm.com/download/win> and keep every default. (If you'd rather skip Git, see "No Git?" below.)
3. **The NVIDIA driver**, if it's older than version 550. Open the **NVIDIA app** (or GeForce Experience), go to Drivers, and update. Without this the model runs on the CPU only: slower, but it still works.

Close PowerShell and open it again so it sees the new programs. Then check:

```powershell
py -3.11 --version     # should print Python 3.11.x
git --version
```

## 2. Download AETHER

Put it in `C:\AETHER`, because the example settings already point there.

**Once PR #8 is merged into `main` on GitHub** (it carries everything from phases 15 to 18):

```powershell
git clone https://github.com/aryankhanna1409/A.E.T.H.E.R C:\AETHER
```

**If you haven't merged it yet**, take the newest branch directly. It already contains everything:

```powershell
git clone -b jarvis-5 https://github.com/aryankhanna1409/A.E.T.H.E.R C:\AETHER
```

**No Git?** On the GitHub page, switch the branch dropdown to `jarvis-5` (or stay on `main` after merging). Click **Code → Download ZIP**, and unzip it so the files sit directly in `C:\AETHER` (you should see `README.md` and `start.bat` there).

## 3. Set it up (one time, about 10 minutes)

```powershell
cd C:\AETHER
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda
```

This creates a private Python environment in `C:\AETHER\.venv`, installs everything (voice, tray and hotkey included), and creates your settings file `aether.toml`. It then installs a **prebuilt** GPU model runner, falling back to the CPU one if needed. You don't need Visual Studio or any compiler. Leave out `-Cuda` if you have no NVIDIA GPU. It's safe to run again at any time: it keeps what's already installed.

## 4. Download the brain and the voice (one time)

```powershell
.\.venv\Scripts\python scripts\get_model.py          # the language model, about 4.7 GB
.\.venv\Scripts\python scripts\get_model.py voice    # the speaking voice, about 60 MB
```

Each command prints the full path of what it downloaded. The model lands in `C:\AETHER\models\` and the voice in `C:\AETHER\models\voices\en\en_GB\alan\medium\`.

If you want faster replies, you can choose a lighter model: `get_model.py small` downloads the 3B model, about 2 GB, and fits entirely on the GPU.

## 5. Tell AETHER about you (2 minutes)

```powershell
notepad aether.toml
```

Change these lines, then save:

```toml
user_name = "Aryan"

[llm]
model_path = "C:/AETHER/models/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"   # what get_model.py printed (part 1)

[security]
allowed_read_roots = ["C:/Users/<you>/Documents/AETHER"]          # folders AETHER may read; add your notes folder
allowed_write_roots = ["C:/Users/<you>/Documents/AETHER/output"]  # where it may create files

[voice]
tts_voice_path = "C:/AETHER/models/voices/en/en_GB/alan/medium/en_GB-alan-medium.onnx"

[online]
location = "Vellore"      # your city, for the weather in the morning briefing
```

Use forward slashes `/` in paths, and replace `<you>` with your Windows user name (it's the folder name under `C:\Users`).

Optional extras:
- **Calendar:** export it as an `.ics` file into `Documents\AETHER\calendar`.
- **Email:** put `.eml` files or an `.mbox` archive into `Documents\AETHER\mail`, or set `outlook = true` under `[calendar]` / `[mail]`.

## 6. Check everything

```powershell
.\.venv\Scripts\python scripts\doctor.py
```

It lists what's fine, what's optional, and the exact fix for anything wrong. Fix any `FAIL` lines, then run it again.

## 7. Start it

Double-click **`C:\AETHER\start.bat`**. AETHER does three things:
- It appears in the system tray (click the `^` arrow next to the clock if you can't see it).
- It loads the model, which takes about a minute the first time.
- It opens the app at <http://127.0.0.1:8765>. If the page doesn't load, wait a moment and refresh.

Things to try:
- **Ctrl+Alt+J** anywhere opens the voice screen. Hold **Space** and say "good morning".
- In chat: "set a timer for 10 minutes for the pasta", "play lofi on youtube", "what's 15% of 2400", "take a note: buy a LiPo", "what's on tomorrow?", "remind me to submit the essay on friday".
- Right-click the tray icon to tick **Start with Windows**, so it's always there.

For hands-free "hey Jarvis" voice (wake word, always listening):

```powershell
.\.venv\Scripts\aether-voice
```

### Teach it your accent (5 minutes, recommended)

Speech-to-text is trained mostly on American and British voices, so it can trip on Indian English. AETHER already tells it to expect Indian English, your name, and the names and places in your memories. To go further, read it 20 short phrases:

```powershell
.\.venv\Scripts\python scripts\learn_my_voice.py
```

It shows what it heard for each phrase and learns the words it gets wrong ("on" becomes "Aundh"). Then you say "hey Jarvis" and "hey Nova" a few times, which tunes the wake word to your voice. It prints how much more accurate it got. Run it again any time.

After that it keeps learning: when the HUD mishears you, click what it heard, fix it and press Enter. When the same fix comes up twice, it remembers it.

You can wake it with **"hey Jarvis"** or just **"hey Nova"**, and say a command in the same breath ("Nova, what's the time?").

### Meet Nova, your desktop companion

When AETHER runs from the tray (`start.bat`), **Nova** appears in the bottom-right corner: a small glowing orb with eyes that stays on top of your windows.

- **Blue** and breathing when idle (it blinks and glances at your mouse pointer).
- **Amber** with wide eyes when listening, and with a spinning ring when thinking.
- **Shifting between blue and amber** while speaking. **Grey and asleep** if AETHER's server isn't running.
- **Click** it to talk. **Drag** it anywhere (it remembers the spot). **Right-click** for: open AETHER, settings, hide for an hour, back to the corner, quit.
- Bring it back with the tray menu's **Nova on screen**. To run it on its own: `.\.venv\Scripts\pythonw -m aether.interface.nova`.

It uses almost no CPU and no GPU. It needs Python's Tk, which the python.org installer includes by default (`doctor.py` checks it).

### Bring your memories from other assistants (optional)

AETHER can start out knowing you. Ask ChatGPT, Claude or Gemini to "list everything you remember about me" (or copy ChatGPT's Settings > Personalization > Manage memories), save the text as a .txt file, and import it:

```powershell
.\.venv\Scripts\python scripts\import_memory.py my_memories.txt
.\.venv\Scripts\python scripts\import_memory.py docs\memory\seed_from_claude.md
```

You see the list first and choose what to keep; nothing is saved until you say yes. ChatGPT's data export (`conversations.json`) works too: only what you said about yourself is offered. In the app, the same thing is under **Memory > Import**, where you can paste or drop a file.

## 8. Make sure it works well on your machine (optional, 15 minutes)

```powershell
.\.venv\Scripts\pytest                        # about 230 checks, no model needed, ~15 s
.\.venv\Scripts\aether-eval                   # 10 real conversations with your model
.\.venv\Scripts\python scripts\bench.py       # speed: time to first word, tokens per second
.\.venv\Scripts\python scripts\tune_gpu.py    # finds the best GPU split for your card
```

## Updating later

```powershell
cd C:\AETHER
git pull
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Cuda
```

## If something goes wrong

| Problem | Fix |
| --- | --- |
| `py` is not recognized | Reinstall Python 3.11 with "py launcher" and "Add to PATH" ticked, then reopen PowerShell |
| `Could not find module ...llama.dll (or one of its dependencies)` | The CUDA runtime DLLs are missing. Run `.\.venv\Scripts\python -m pip install "nvidia-cuda-runtime-cu12==12.4.*" "nvidia-cublas-cu12==12.4.*"` (setup does this now). If it still fails, install the [Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) and reopen PowerShell. You don't need the CUDA Toolkit |
| `nmake` / `CMAKE_C_COMPILER not set` / "Failed building wheel for llama-cpp-python" | An older setup script tried to compile the model runner. Run `git pull`, then run step 3 again. It now only uses prebuilt files |
| `No module named 'huggingface_hub'` | The package install stopped early. Run `git pull`, then run step 3 again |
| "running scripts is disabled" | Use the exact command in step 3 (it includes `-ExecutionPolicy Bypass`) |
| Replies are very slow | Run `doctor.py`. If it says "CPU only", redo step 3 with `-Cuda` and update the NVIDIA driver. Or use `get_model.py small` |
| "Out of memory" / crash on load | In `aether.toml`, lower `[llm] gpu_layers` (try 16), or run `scripts\tune_gpu.py` |
| No sound / it doesn't hear me | Windows Settings → Privacy & security → Microphone → allow desktop apps. Check `doctor.py` |
| Weather or news missing | Set `[online] location`, and make sure `[security] network = true` |
| The page at 127.0.0.1:8765 won't load | Wait a minute on first start. If it still fails, run `.\.venv\Scripts\aether-server` in PowerShell to see the error |

Everything AETHER knows about you lives in `%LOCALAPPDATA%\AETHER` (encrypted; paste that into the Explorer address bar to open it). To start fresh, delete that folder.
