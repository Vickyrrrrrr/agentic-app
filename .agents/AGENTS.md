# Agent Design and File Naming Guidelines

To maintain a clean, readable, and structured codebase, the agent must adhere to the following naming conventions when creating files, folders, or proposing sessions:

## 1. Naming Conventions for Files and Folders
* **Be Concise and Structured:** Always use short, technical, and to-the-point names. Avoid conversational phrases, questions, typos, or sentences (e.g., do NOT name files/folders like `you_tell_me_where_i_can_fabricate` or `confirm_builder_mode_for_aes_implementation`).
* **Format:** Use lowercase `kebab-case` for documents, scripts, and workflows (e.g., `aes-core`, `sta-run-report`, `sram-wrapper-spec`). Use `snake_case` or `kebab-case` consistently based on the PDK or tool conventions.
* **Keep it short:** Aim to keep file and folder names under 24 characters.

## 2. Directory Structure
* Organize files logically under dedicated directories:
  * RTL files under `rtl/`
  * Testbenches under `tb/`
  * Synthesis and timing logs/reports under `reports/` or `logs/`
  * Documents under `docs/`

# Agent working rules

* Verify paths, files, and command outputs against the real filesystem. Never fake results or assume files exist.
* For complex or ambiguous prompts, ask clarifying questions first.
* No auto-push: never run `git push` unless the user explicitly asks.
