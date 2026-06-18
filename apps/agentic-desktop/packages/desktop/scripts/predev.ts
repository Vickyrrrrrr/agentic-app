import { $ } from "bun"
import path from "node:path"

await $`bun ./scripts/copy-icons.ts ${process.env.OPENCODE_CHANNEL ?? "dev"}`

const buildNodeScript = path.resolve(import.meta.dir, "../../opencode/script/build-node.ts")
await $`bun ${buildNodeScript}`
