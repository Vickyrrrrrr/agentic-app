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
  PDK files on your disk, finds the real cause, and proposes a fix.
  This is the loop that takes a human hours; AgentIC does it in
  seconds.
- **Try ideas quickly.** Spinning up a new variant of a block is
  as cheap as typing a new sentence. The agent can fork a design,
  swap a parameter, re-run the flow, and show you the new area /
  timing / power numbers.
- **Learn the flow by watching it run.** If you are new to digital
  design, AgentIC is a teacher that does the work in front of you
  and explains what it did. Every tool call, every PDK read, every
  flow step is logged and visible.
- **Bring your own models.** AgentIC works with any OpenAI-compatible
  model provider. Use the hosted one for quick experiments, or plug
  in your own key for the model you trust with your IP.

---

## How is AgentIC different from other AI tools?

Most AI tools for chip design are cloud-hosted and assume you are
willing to upload your source. AgentIC takes the opposite bet.

- **Your chip never leaves your machine.** The agent runs the
  EDA flow locally. Source code, testbenches, waveforms, the GDS —
  all on your disk, all under your control. The cloud is only used
  to verify that your license is valid and to count how many builds
  you ran this month. It never sees prompts, source, or PDKs.
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
- **It is a desktop app, not a web IDE.** You do not need to
  upload a project to "the cloud workspace". You open the app, you
  point it at your local workspace, and it works on the files
  already on your machine. It feels like VS Code for chips, not
  like Colab for chips.
- **It is autonomous, not chatty.** You do not have to babysit
  every step. Send a prompt, walk away, and come back to a finished
  layout. The agent iterates on its own: explore, write, run,
  read errors, fix, repeat — up to 20 rounds per request.
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
- Use your own model key, or the hosted one, depending on what
  you trust with your work.
- Auto-update to new releases over GitHub without reinstalling.

---

## What it does not do (yet)

- **Analog, RF, or custom layout.** AgentIC is for digital
  RTL → GDSII only.
- **Replace your tapeout review.** The agent scaffolds and
  iterates; a human signs off before fabrication.
- **Run without a license.** Even with your own model key, you
  need an active AgentIC subscription.
- **Touch third-party IP you have not loaded.** AgentIC works on
  files inside your workspace. If you have not given it access to
  a piece of IP, it does not see it.

---

## This is the first release. More is coming.

AgentIC v1.0 is the starting line, not the finish line. The first
public release ships with the core agent loop, the local backend,
the desktop app, the design studio, the workspace, the model
integration, and the license / billing / deep-link plumbing.

Things on the near-term roadmap (subject to change, but real):

- Better PDK auto-detection and "first-run" flow for new
  workspaces.
- More visualization: layout viewers, waveform inspectors,
  flow-stage progress.
- Multi-design project support.
- More model providers and routing presets.
- Tighter integration with leading commercial EDA tools.
- A friendlier setup experience for users who have never
  installed a flow before.

If you have a feature you want to see, file an issue or send a
note. The roadmap is shaped by what real chip designers ask for.

---

## Where to next?

- New to chip design? Start with the design studio, pick a small
  block, and watch the agent walk through the flow.
- Working chip engineer? Point AgentIC at a PDK you already use
  and try it on a block you have done before. Compare the result
  to your hand-rolled flow.
- Student? Use the open-source EDA stack and a free PDK to follow
  the full RTL → GDSII path on your laptop.

Whatever path you take, AgentIC's job is the same: handle the
repetitive plumbing so you can spend more time on the design.
