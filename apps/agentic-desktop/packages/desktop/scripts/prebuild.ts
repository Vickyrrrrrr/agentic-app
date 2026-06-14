#!/usr/bin/env bun
import { $ } from "bun"
import path from "node:path"

import { resolveChannel } from "./utils"

const channel = resolveChannel()
await $`bun ./scripts/copy-icons.ts ${channel}`
await $`bun ./scripts/copy-metainfo.ts ${channel}`

const buildNodeScript = path.resolve(import.meta.dir, "../../opencode/script/build-node.ts")
await $`bun ${buildNodeScript}`
