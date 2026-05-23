# AgentIC

AgentIC is a specialized development environment for digital circuit design and simulation, built around an autonomous AI coding agent. 

---

## What is Agentic Development?

Traditional AI assistants are passive—they generate code snippets in a chat interface for the user to manually copy, paste, compile, and debug. 

In AgentIC, the AI agent is an active participant in the environment. Given a set of high-level goals, it is equipped with a secure execution sandbox and a suite of interactive tools to:
* **Navigate and Modify**: Read, write, and refactor files directly within the workspace.
* **Verify Compilations**: Execute terminal commands and trigger syntax/lint checks using digital design compilers (such as Verilator).
* **Diagnose and Self-Correct**: Parse compiler warning and error logs to locate, modify, and fix code recursively until compilation succeeds.
* **Engage Safety Controls**: Propose file modifications and terminal commands through a visual diff review, requiring user approval before execution (Human-in-the-Loop flow).

---

## What this Application Serves

This repository contains the split client frontends of the AgentIC architecture, organized as a monorepo containing:
* **Web Client (`/web`)**: A React + Vite interface deployable to cloud hosts like Vercel. It manages user workspaces, auth, and database persistence through Supabase, while delegating EDA and compilation tasks to a remote server.
* **Desktop Client (`/desktop`)**: An Electron-based desktop application. It runs the same interface with elevated system permissions, allowing it to interface directly with local file systems and system-installed EDA binaries (Verilator, Yosys, GTKWave) without relying on a remote API server.



