# AgentIC: Local VLSI Design Agent Workspace

AgentIC is an interactive development environment (IDE) and local runtime orchestration system built specifically for AI-driven chip design (VLSI engineering). It bridges standard Large Language Models (LLMs) with local Electronic Design Automation (EDA) tools and Process Design Kit (PDK) stacks.

---

## Why AgentIC? (The Problem it Solves)

Digital circuit design (RTL-to-GDSII) is an extremely complex, highly iterative, and data-heavy process. A single chip design run requires coordinating dozens of specialized EDA compilers (Yosys, OpenROAD, Verilator, etc.), matching design logic to local PDK cell libraries (Sky130, ASAP7), and debugging physical violations across gigabytes of log outputs.

Traditional software engineering approaches and generic coding agents fail in this domain because chip design is not just "writing code"—it is about meeting strict physical constraints (Timing, Power, Area, DRC, and LVS).

---

## AgentIC vs. Generic Coding Agents (Why a `SKILL.md` is Not Enough)

A common question is: **"Why can't I just use standard LLMs (like Claude, GPT, or Codex) equipped with a `SKILL.md` instruction file?"**

While a `SKILL.md` file tells a model *how* to run a hardware design tool, a raw model still lacks the infrastructure required to actually execute it. Here is how AgentIC's local backend runtime solves this:

| Capability | Standard Agent + `SKILL.md` | AgentIC Workspace |
| :--- | :--- | :--- |
| **Log Management** | Standard agents dump raw terminal logs into the LLM context. A single compilation or DRC run can output **hundreds of megabytes** of logs, quickly exhausting context windows and causing hallucinations. | The AgentIC backend runs local python parsers (`report_parsers.py`, `sta_reports.py`) to compress gigabytes of log output, extracting and presenting only the precise timing violations or physical coordinates to the model. |
| **State Recovery** | If aPlace-and-Route (PnR) run fails after 30 minutes, the workspace is left in an unstable state. Standard agents cannot easily undo partial tool steps. | The backend maintains a **durable Checkpoint Engine**. If a design stage fails, the agent can immediately rollback the workspace filesystem and DB state to a previous successful stage and try a different strategy. |
| **PDK Integration** | A raw LLM does not know which physical cells or macros (e.g. SRAM, IO buffers) actually exist on your local machine, leading it to invent non-existent hardware. | The local runtime actively **indexes your PDK libraries** (LEF/LIB files). The agent queries this capability graph to verify cell availability before writing RTL or configuring synthesis. |
| **Domain-Specific Logic** | Generic agents treat all terminal outputs as plain text. They have no understanding of VLSI-specific logic constraints. | AgentIC has built-in **validation schemas** and **contract checking** to enforce hardware budgets (e.g., negative slack, clock constraints) and verify compliance at each step of the flow. |

---

## Key Capabilities

*   **RTL Linter & Auto-Repair:** Automatically analyzes Verilog/SystemVerilog using local compilers and repairs syntax or synthesis bugs.
*   **Static Timing Analysis (STA) Parser:** Pinpoints timing, setup/hold, and critical path violations from complex timing reports.
*   **Physical Verification Parsing:** Extracts DRC (Design Rule Check), LVS (Layout vs. Schematic), and Antenna violations from log files.
*   **PDK and Capability Mapping:** Reads and indexes local PDK cell libraries to ground the agent's hardware decisions in reality.

---

## Who is AgentIC For?

*   **Digital IC Design Engineers:** Looking to automate repetitive linting, constraint tuning, and debug loops in RTL-to-GDSII flows.
*   **Hardware Prototypers:** Who want rapid, closed-loop compiler and simulation feedback when developing synthesizable hardware.
*   **EDA & CAD Developers:** Seeking to test, benchmark, and optimize tool flow configurations autonomously.
