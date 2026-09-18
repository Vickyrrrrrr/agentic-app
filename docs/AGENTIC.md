# AgentIC

> A local AI agent that helps you go from a chip idea to a
> fabrication-ready layout — on your own machine.

---

## What is AgentIC?

AgentIC is a desktop application that puts an AI design engineer on
your workstation. You describe a digital block in plain English —
something like *"an AXI timer peripheral with formal verification"* or
*"8-bit RISC-V control unit, optimized for area"* — and AgentIC
writes the Verilog, runs simulation, synthesis, place-and-route, and
signoff, and reports back with a layout you can take to a foundry.

It is not a chatbot that pastes code. It is a long-running agent that
actually drives the EDA tools installed on your computer, reads your
PDK, fixes its own mistakes, and gets to a clean result.

You stay in control: you see every file it writes, every command it
runs, and every error it hits. You can stop it mid-flight, steer it
with a new message, or take over and edit the Verilog yourself.

---

## How does AgentIC help with chip design?

Whether you are a working chip designer or a student learning
digital design, AgentIC removes the parts of the flow that are
tedious, repetitive, and error-prone.

- **Stop hand-writing the boilerplate.** Testbenches, constraints,
  flow scripts, project layouts — the agent scaffolds the project
  structure for you, so you can focus on the design itself.
- **Skip the back-and-forth between tools.** You no longer have to
  paste the same `yosys`/`iverilog`/`openroad` command lines in and
  out of a terminal. The agent chains the right commands in the
  right order and reads the output of each one.
- **Get unstuck on errors faster.** When synthesis or P&R fails
  with a cryptic cell-name or layer-rule error, the agent reads the
  PDK files on your disk, finds the cause, and proposes a fix.
- **Try ideas quickly.** Spinning up a new variant of a block is
  as cheap as typing a new sentence. The agent can fork a design,
  swap a parameter, re-run the flow, and show you the new area /
  timing / power numbers.
- **Learn the flow by watching it run.** If you are new to digital
  design, AgentIC is a teacher that does the work in front of you
  and explains what it did. Every tool call, every PDK read, every
  flow step is logged and visible.
- **Bring your own models.** AgentIC works with any OpenAI-compatible
  model provider via your own key.

---

## How is AgentIC different from other AI tools?

Most AI tools for chip design are cloud-hosted and assume you are
willing to upload your source. AgentIC takes the opposite bet.

- **Your chip never leaves your machine.** The agent runs the
  EDA flow locally. Source code, testbenches, waveforms, the GDS —
  all on your disk, all under your control. There is no account,
  no license server, no usage counting. The only network traffic
  is your own model API calls, if you configure a model key.
- **It works with the tools you already have.** Whether you are
  using the open-source EDA stack (Yosys, Verilator, OpenROAD,
  OpenLane, Magic, Klayout) or commercial EDA tools from leading
  vendors, AgentIC discovers what's installed and adapts. Same for
  PDKs: open-source libraries, university PDKs, or your foundry
  NDA library — if it is on your disk, the agent can read it and
  use it.
- **It reads your PDK, it does not guess.** Before the agent
  writes any cell name, layer rule, or timing corner, it reads the
  real `.lib`/`.lef`/`.tcl` files in your PDK. This makes its
  output usable in a real flow instead of something that looks
  right but breaks at signoff.
- **It is a desktop app, not a web IDE.** No project upload.
  Point it at a local workspace and it works on the files there.
- **It iterates on its own.** Explore, write, run, read errors,
  fix, repeat — up to 20 rounds per request without babysitting.
- **It is open about what it ran.** Every tool invocation, every
  PDK read, every command line is logged. There is no hidden
  remote execution. If you want to reproduce a build, you can.

---

## What can you do with it today?

- Generate synthesizable Verilog from a natural-language spec.
- Run a full RTL → GDSII flow on a real PDK and get a layout
  file out.
- Build and run testbenches, dump waveforms, and inspect them
  in-app.
- Iterate on a block: change a parameter, re-run, compare
  results.
- Use it as a teaching tool: watch a real flow run, see what
  each step does, inspect the artifacts.
- Bring your own model key for the agent loop. Deterministic
  stages (lint, PDK indexing, report parsing, flow routing) need
  no model at all.
- Auto-update to new releases over GitHub without reinstalling.

---

## What it does not do (yet)

- **Analog, RF, or custom layout.** AgentIC is for digital
  RTL → GDSII only.
- **Replace your tapeout review.** The agent scaffolds and
  iterates; a human signs off before fabrication.
- **Touch third-party IP you have not loaded.** AgentIC works on
  files inside your workspace. If you have not given it access to
  a piece of IP, it does not see it.

---

## Where to next?

- New to chip design? Start with the design studio, pick a small
  block, and watch the agent walk through the flow.
- Working chip engineer? Point AgentIC at a PDK you already use
  and try it on a block you have done before. Compare the result
  to your hand-rolled flow.
- Student? Use the open-source EDA stack and a free PDK to follow
  the full RTL → GDSII path on your laptop.

Whatever path you take, AgentIC handles the repetitive flow
plumbing.
