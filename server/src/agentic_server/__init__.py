"""AgentIC local engine. UI processes are thin clients; this package owns truth.

Import direction (review-enforced):
  api -> agent, vlsi, runtime, schemas | agent -> vlsi, runtime, schemas
  vlsi -> schemas (+ stdlib; pydantic for lint) | runtime -> vlsi, schemas
  schemas, config -> nothing in-package
"""
