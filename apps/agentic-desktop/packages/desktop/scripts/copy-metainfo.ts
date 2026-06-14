import { resolveChannel } from "./utils"

const arg = process.argv[2]
const channel = arg === "dev" || arg === "beta" || arg === "prod" ? arg : resolveChannel()

const appId = channel === "prod" ? "live.buildstack.agentic" : `live.buildstack.agentic.${channel}`
const productName = channel === "prod" ? "AgentIC" : `AgentIC ${channel.charAt(0).toUpperCase() + channel.slice(1)}`
const summary = `Local VLSI agent workspace${channel !== "prod" ? ` (${channel})` : ""}`

const xml = `<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>${appId}</id>

  <metadata_license>CC0-1.0</metadata_license>
  <project_license>MIT</project_license>

  <name>${productName}</name>
  <summary>${summary}</summary>

  <developer id="live.buildstack">
    <name>AgentIC</name>
  </developer>

  <description>
    <p>
      AgentIC is a local VLSI agent workspace for planning, editing, and running chip design flows.
    </p>
  </description>

  <launchable type="desktop-id">${appId}.desktop</launchable>

  <content_rating type="oars-1.1" />

  <url type="bugtracker">https://github.com/Vickyrrrrrr/agentic-app/issues</url>
  <url type="homepage">https://buildstack.live/agentic</url>
  <url type="vcs-browser">https://github.com/Vickyrrrrrr/agentic-app</url>

  <screenshots>
    <screenshot type="default">
      <image>https://buildstack.live/agentic/desktop-preview.png</image>
    </screenshot>
  </screenshots>
</component>
`

await Bun.write(`resources/${appId}.metainfo.xml`, xml)
console.log(`Generated metainfo for ${channel} at resources/${appId}.metainfo.xml`)
