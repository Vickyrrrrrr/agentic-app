import { $ } from "bun"
import path from "node:path"
import fs from "node:fs"

await $`bun ./scripts/copy-icons.ts ${process.env.OPENCODE_CHANNEL ?? "dev"}`

// Copy license.json if it exists in the root desktop project resources
const rootLicensePath = path.resolve(import.meta.dir, "../../../../../desktop/resources/license.json")
const destLicensePath = path.resolve(import.meta.dir, "../resources/license.json")
if (fs.existsSync(rootLicensePath)) {
  console.log(`Copying license.json from ${rootLicensePath} to ${destLicensePath}`)
  fs.copyFileSync(rootLicensePath, destLicensePath)
} else {
  console.log(`license.json not found at ${rootLicensePath}, skipping copy.`)
}

const buildNodeScript = path.resolve(import.meta.dir, "../../opencode/script/build-node.ts")
await $`bun ${buildNodeScript}`
