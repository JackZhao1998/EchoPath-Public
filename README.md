<div align="center">

<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/echopath-wordmark-dark.svg">
    <img src="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/echopath-wordmark-light.svg" width="300" height="72" alt="EchoPath">
  </picture>
</h1>

<p><strong>Windows version</strong> &nbsp;&middot;&nbsp; <a href="https://github.com/JackZhao1998/EchoPath-Public/tree/main">macOS version →</a></p>

<p><strong>Good work, on repeat.</strong><br>
Desktop workflows your agent can learn and reuse.</p>


<p>
  <a href="document/mcp_architecture.md"><img src="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/badge-mcp.svg" height="28" alt="MCP compatible"></a>
  <a href="INSTALL.md"><img src="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/badge-python.svg" height="28" alt="Python 3.10 or newer"></a>
  <a href="LICENSE"><img src="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/badge-license.svg" height="28" alt="MIT license"></a>
</p>

<p>
  <sub>A joint work of <a href="https://engineering.jhu.edu/ams/" title="Johns Hopkins University Department of Applied Mathematics and Statistics">JHU Applied Mathematics &amp; Statistics</a> and <a href="https://amazon.jobs/content/en/teams/agi" title="Amazon Artificial General Intelligence">Amazon AGI</a></sub>
</p>

<p>
  <a href="#installation"><strong>Get started →</strong></a> ·
  <a href="#watch-echopath">Watch the film</a> ·
  <a href="#software-demos">Software demos</a> ·
  <a href="#documentation">Documentation</a>
</p>

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/echopath-hero-dark.svg">
  <img src="https://github.com/JackZhao1998/EchoPath-Public/raw/main/document/assets/echopath-hero-light.svg" width="1600" alt="A good run today. A head start tomorrow. Complete a task, keep its successful steps and visual anchors, and reuse the workflow with checks on the current screen.">
</picture>

EchoPath runs tasks across desktop apps, turns successful runs into local
workflow memories, and replays what worked with visual checks. Bring your
existing model backend, work on your desktop or in a VM, and follow progress
from the Control Room.

| **Across your apps** | **Your model backend** | **Local workflow memory** |
| --- | --- | --- |
| Operate a desktop or connected VM, with pause, resume, and stop controls. | Use an existing Codex, Claude Code, Kimi, Ollama, or command-based setup. | Keep successful steps, screen anchors, and task parameters for the next run. |

## Watch EchoPath

### Good work, on repeat · 0:36

A 36-second introduction to reusable workflows, desktop and VM options,
specialist tools, and everyday tasks. Play the film directly below.

https://github.com/user-attachments/assets/38a42ba6-9bce-48f5-bbac-ca1a377b9bac

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/introduction/EchoPath-introduction-en.vtt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/introduction/EchoPath-commercial-bright-36s.mp4)

## How it works

1. **Run a task.** Describe the goal. EchoPath observes and operates the target
   desktop through your configured model backend.
2. **Keep what worked.** Successful GUI runs are consolidated into reusable
   workflows, with saved steps, screen anchors, and task parameters.
3. **Replay next time.** Reuse a matching workflow with visual re-aiming and
   checks. When replay cannot continue, the default is to fall back to planning.

The backend can use your existing Codex, Claude Code, Kimi, Ollama, or other
command-based setup. EchoPath exposes its runtime through MCP, so the coding
agent you use and the model backend that plans tasks can be different.

Replay benefits depend on the task, environment, and workflow. The
[flight-search case study](document/united_flight_demo.md) documents one
prepared replay, including timings, token estimates, learning, and repair costs.

## From specialist tools to everyday tasks

| Everyday workflows | Specialist workflows |
| --- | --- |
| **Shopping:** compare products and organize options. | **Imaging:** work with 3D Slicer and ImageJ. |
| **Travel planning:** research trips and compare choices. | **Finance and engineering:** use LibreOffice Calc and FreeCAD. |
| **File organization:** sort documents and repeat familiar steps. | **Mapping and audio:** work with QGIS and Audacity. |

These are illustrative workflow areas. The recorded demos below show specific
completed tasks in specialist apps.

## Software demos

Recorded desktop tasks in a Windows VM, using public or synthetic inputs.
Actions play at normal speed; setup, waiting, and recovery periods are cut.
These are planned runs, not replay benchmarks.

<details>
<summary><strong>Medical imaging · 3D Slicer</strong> — 1:24</summary>

Explore a chest CT in 2D and 3D, then save the scene and data as a bundle.

https://github.com/user-attachments/assets/e509627c-a4bf-4539-a4e8-75e0f08c896e

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/slicer/echopath-slicer-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/slicer/echopath-slicer-demo.mp4)

</details>

<details>
<summary><strong>Image analysis · ImageJ</strong> — 1:25</summary>

Analyze particles in a calibrated image; export measurements and numbered outlines.

https://github.com/user-attachments/assets/659a8210-78ce-4f7b-a05f-c9ba83eacba7

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/imagej/echopath-imagej-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/imagej/echopath-imagej-demo.mp4)

</details>

<details>
<summary><strong>Finance · LibreOffice Calc</strong> — 1:27</summary>

Flag invoice variances and duplicates, chart vendor totals, and save an editable workbook.

https://github.com/user-attachments/assets/d11f2d9e-84c1-4107-82f9-abe8e0d6b603

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/calc/echopath-calc-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/calc/echopath-calc-demo.mp4)

</details>

<details>
<summary><strong>Engineering · FreeCAD</strong> — 1:21</summary>

Model a flanged bushing with a through bore; save the native model and export STEP.

https://github.com/user-attachments/assets/dacec34d-66b9-4c62-b0ad-e2b7f65608ad

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/freecad/echopath-freecad-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/freecad/echopath-freecad-demo.mp4)

</details>

<details>
<summary><strong>Mapping · QGIS</strong> — 1:29</summary>

Map clinic service areas, count covered households, and save the project and analysis layers.

https://github.com/user-attachments/assets/6ea890c3-0b52-449c-bc35-13d91d058119

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/qgis/echopath-qgis-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/qgis/echopath-qgis-demo.mp4)

</details>

<details>
<summary><strong>Audio production · Audacity</strong> — 1:24</summary>

Filter low-frequency rumble, remove DC offset, normalize audio, and save a WAV file and editable project.

https://github.com/user-attachments/assets/735affcc-1e3b-414f-9b7d-570ff8a5aa98

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/audacity/echopath-audacity-demo.srt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/demos/audacity/echopath-audacity-demo.mp4)

</details>

[All finished videos and captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/README.md)

## Installation

### Installation walkthrough · macOS · 4:33

Set up the Mac app, connect your coding agent, and follow the optional
Linux/macOS guest setup.

https://github.com/user-attachments/assets/bb69b056-6703-49f8-bddc-d4214874ce80

[Captions](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/installation/EchoPath-installation-macOS-en.vtt) ·
[Setup commands](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/installation/Setup-commands.txt) ·
[Video file](https://github.com/JackZhao1998/EchoPath-Public/blob/main/media/installation/EchoPath-installation-macOS.mp4)

The recording predates this public repository; use the current installation
commands below and the updated companion setup commands.
The walkthrough covers macOS. For Windows, use the
[Windows app and VM guide](document/windows_setup.md).

This branch provides the **Windows tray app and managed VirtualBox guest setup**.
Use Windows 11 x64, Python 3.10+ with `venv` and pip, and a signed-in Codex or
Claude Code CLI. Keep the source folder in a permanent location.

### With your coding agent

Share this branch or its downloaded folder, then ask:

```text
Install EchoPath from https://github.com/JackZhao1998/EchoPath-Public.git using branch windows-version.
Read INSTALL.md and follow packaging/agents/echopath-setup/SKILL.md.
Connect this coding agent using my existing model login and include the Windows
tray app. Leave VM integration, JEV, and recording off for now.
```

### From a terminal

In PowerShell, clone the Windows branch into a new permanent folder:

```powershell
git clone --branch windows-version --single-branch https://github.com/JackZhao1998/EchoPath-Public.git EchoPath
Set-Location EchoPath
python packaging/setup.py install --backend codex --agent codex --app
```

Use `py -3` if Python is installed through the Windows launcher. For Claude
Code, use `--backend claude --agent claude`. If you already have this branch,
run the installation command from its source folder without cloning again.

The helper creates a private environment, configures the backend, verifies MCP,
and connects the selected agent and skills. **Restart your coding agent after
setup**. Open **EchoPath** from the Start menu for tray controls and the browser
Control Room. Keep the Windows desktop signed in and unlocked.

See [INSTALL.md](INSTALL.md) for verification, updates, and repair, and the
[Windows guide](document/windows_setup.md) for Windows guest creation.
The [macOS release](https://github.com/JackZhao1998/EchoPath-Public/tree/main) has its own native-app setup instructions.

## Your first task

In Codex, invoke `$echopath`; in Claude Code, invoke `/echopath`, followed by a
small task with a clear stopping point:

> Use EchoPath on this computer to open my browser, search for EchoPath on
> GitHub, and stop at the search results.

You can also start from the Control Room: choose **This computer** or
**Virtual machine**, describe the task, and enable **Allow desktop control**.
Watch the activity; **Pause** and wait for **Paused** before using that desktop
manually. Resume to continue, or Stop to end the task.

After a successful run, open **Stored procedures**, inspect the learned
workflow, and use it as the basis for the next task.

## Desktop and VM support

| Desktop | What you need |
| --- | --- |
| **macOS** | Use the [macOS release](https://github.com/JackZhao1998/EchoPath-Public/tree/main) and its native app with Accessibility and Screen Recording permissions. |
| **Windows** | An active, signed-in, unlocked desktop; native tray controls and the web Control Room are available. |
| **Linux** | A graphical X11 session, Tk, `scrot`, and clipboard support such as `xclip`; use the CLI or web Control Room. |

For a separate workspace, connect a guest desktop:

- **Linux on a Mac:** managed creation with Lima.
- **macOS on an Apple-silicon Mac:** create a Tart guest, then link it over SSH.
- **Windows on Windows:** managed creation with VirtualBox and a Windows x64 ISO.
- **Existing desktops:** connect a prepared Linux/macOS guest over SSH, or use
  a configured desktop bridge.

Setup routes and platform-specific limits are in the
[VM guide](document/vm_setup.md) and [Windows guide](document/windows_setup.md).
VM integration starts off. The installation walkthrough shows macOS setup;
it does not cover every guest or platform feature.

## Data and control

Task history, workflow memories, and recordings are stored locally.
Prompts, planning context, and screenshots are sent to your selected model
backend. Optional JEV sends screen text to OpenRouter; it starts disabled,
as do VM integration and task video recording.

MCP desktop control requires explicit opt-in for each task. Pause, resume, and
stop are cooperative, so an in-flight action or model call may finish before
the requested state takes effect. The standalone CLI uses the configured
skills by default; use `--no-desktop-control` for tasks that should not use GUI
control.

See [local data and privacy](https://github.com/JackZhao1998/EchoPath-Public/blob/main/document/usage.md#local-data-and-privacy) for storage
locations, backend boundaries, and advanced controls.

## Documentation

| Guide | Covers |
| --- | --- |
| [Installation](INSTALL.md) | Prerequisites, agent setup, verification, updates, and repair. |
| [Using EchoPath](https://github.com/JackZhao1998/EchoPath-Public/blob/main/document/usage.md) | Control Room, model bridges, MCP tasks, CLI, memory, and troubleshooting. |
| [VM setup](document/vm_setup.md) | Linux and macOS guests, SSH connections, and desktop bridges. |
| [Windows setup](document/windows_setup.md) | Windows branch selection, tray app, VirtualBox guests, and manual takeover. |
| [Model bridges](bridges/README.md) | Available backends, authentication, and the bridge contract. |
| [MCP architecture](document/mcp_architecture.md) | Tools, resources, prompts, and the runtime boundary. |
| [Segmented replay](document/native_segments.md) | Optional host-assisted native steps inside a learned workflow. |

## Contributing

Bug reports, documentation fixes, new model bridges, and reproducible workflow
examples are welcome. In an [issue](https://github.com/JackZhao1998/EchoPath-Public/issues),
include the operating system, backend, steps to reproduce, and sanitized logs.

Core code lives in [`src/echopath/`](src/echopath/). Local regression suites in
`test/` and `tests/` are excluded from this release branch. See
[development notes](https://github.com/JackZhao1998/EchoPath-Public/blob/main/document/usage.md#development) for unit checks and MCP
inspection, or start a new backend from the
[bridge template](bridges/_template/README.md). Keep credentials, local
configuration, and private task data out of contributions.

Release details and verification steps are in [RELEASE.md](RELEASE.md);
changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE).
