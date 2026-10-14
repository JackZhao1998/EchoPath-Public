# Agent Flow For A New Task

This document shows how the current EchoPath agents and services work together when a new user task arrives.

## End-To-End Flow

```mermaid
flowchart TD
    A["User sends a new message"] --> B["EchoPathChatService<br/>creates or reuses a chat session"]
    B --> C["Chat service builds an OrchestratorAgent<br/>and wires router, skills, desktop service, replay store"]
    C --> D["Session debug state starts<br/>and event capture begins"]
    D --> E["OrchestratorAgent"]

    E --> F["ShortTermMemoryAgent<br/>phase = intake"]
    F --> G{"Replay match found<br/>exactly or through<br/>a bound consolidated memory?"}

    G -->|Yes| H["Replay store returns the latest<br/>successful task payload"]
    H --> I["Orchestrator skips fresh routing plan<br/>and reuses the stored task payload"]

    G -->|No| J["PlanningAgent<br/>classifies the request into a task type"]
    J --> K["ShortTermMemoryAgent<br/>phase = planned"]

    I --> L{"Task type"}
    K --> L

    L -->|desktop_task| M["GUIAgent"]
    L -->|command_task| N["ToolAgent"]
    L -->|file_task| N
    L -->|none| O["Return planner reply only"]

    M --> S["Desktop workflow"]

    S --> T{"Desktop payload has explicit actions?"}
    T -->|Yes| U["ExecutionAgent runs<br/>pyautogui-desktop scripted actions"]
    T -->|No| V["Observe desktop"]
    V --> W["PlanningAgent<br/>plans the next GUI step"]
    W --> X{"Grounding needed?"}
    X -->|Yes| Y["ImageGroundingAgent<br/>finds exact coordinates"]
    X -->|No| Z["Use planned action directly"]
    Y --> AA["ExecutionAgent runs<br/>pyautogui-desktop action"]
    Z --> AA
    AA --> AB["Observe desktop again"]
    AB --> AC{"Completed or halted?"}
    AC -->|Loop| W
    AC -->|Done| AD["Desktop execution result"]

    N --> AE["ExecutionAgent runs the matched skill<br/>command-line or workspace-files"]

    AD --> AF
    AE --> AF
    O --> AG["Final orchestrator output"]
    AF --> AG

    AG --> AH["ShortTermMemoryAgent<br/>phase = completed"]
    AH --> AI["LongTermMemoryAgent<br/>phase = completed"]
    AI --> AJ["InteractionLogger writes task, raw actions,<br/>screenshots, replay metadata, and events"]
    AJ --> AK["Chat service updates session messages<br/>and live debug panel state"]
    AK --> AL["Assistant reply is returned to the UI"]
```

## Agent Responsibilities

- `EchoPathChatService` is the product entry point. It owns sessions, debug state, and wiring of the orchestrator with skills, desktop control, and replay storage.
- `OrchestratorAgent` is the coordinator. It updates memory agents, checks replay first, asks for a routing decision when needed, then delegates execution by `task.type`.
- `PlanningAgent` handles two planning jobs:
  - route the incoming request into `desktop_task`, `command_task`, `file_task`, or `none`
  - plan the next desktop step during iterative GUI execution
- `GUIAgent` owns visible desktop work, including browser workflows in a normal browser window.
- `ToolAgent` owns command and file work.
- `ImageGroundingAgent` is only used when a desktop plan needs visual target localization before clicking or moving.
- `ExecutionAgent` is the common executor that actually invokes skills.
- `ShortTermMemoryAgent` and `LongTermMemoryAgent` are called around the orchestration lifecycle so memory can be updated before and after execution.

## Desktop Loop Detail

The desktop path is the most agentic path in the current system.

```mermaid
flowchart LR
    A["Observe current desktop"] --> B["Plan next GUI step"]
    B --> C{"Need visual grounding?"}
    C -->|Yes| D["Ground target to pixels"]
    C -->|No| E["Use direct action"]
    D --> F["Execute action"]
    E --> F
    F --> G["Observe again"]
    G --> H{"Goal completed?"}
    H -->|No| B
    H -->|Yes| I["Return desktop result"]
```

In practice this is:

- `observe`
- `plan`
- `ground` when needed
- `act`
- `observe again`
- repeat until completed, halted, or max iterations are reached

## Replay Path Detail

Replay does not remove orchestration. It changes where the orchestrator gets the routed task.

```mermaid
flowchart LR
    A["New user query"] --> B["Orchestrator checks InteractionReplayStore"]
    B --> C{"Exact or parameter-bound<br/>successful match exists?"}
    C -->|Yes| D["Reuse or adapt the task payload"]
    C -->|No| E["Ask PlanningAgent to create a fresh task"]
    D --> F["Route to GUIAgent or ToolAgent"]
    E --> F
```

That means:

- planning still exists as part of the system design
- the orchestrator stays responsible for deciding whether a task is replayable
- when a replay hit exists, the orchestrator can skip planning from scratch and go directly into execution with the logged task
- consolidated memories are checked before raw action trajectories when both are available
- generalized consolidated memories can be rebound to current request inputs before replay; for example, the `arxiv_paper_download` memory accepts an arXiv paper URL, arXiv ID, or title and substitutes the current `search_url` and `pdf_url`
