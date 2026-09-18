import { Component } from "solid-js"
import { Tag } from "@opencode-ai/ui/v2/badge-v2"
import { SettingsListV2 } from "./parts/list"
import { SettingsRowV2 } from "./parts/row"
import "./settings-v2.css"

// Local mode: no accounts, no license server, no billing. The backend always
// runs locally with full access, so this page is informational only.
export const SettingsProfileV2: Component = () => {
  return (
    <>
      <div class="settings-v2-tab-header">
        <h2 class="settings-v2-tab-title">Profile</h2>
      </div>

      <div class="settings-v2-tab-body">
        <div class="settings-v2-section">
          <h3 class="settings-v2-section-title">AgentIC account</h3>
          <SettingsListV2>
            <SettingsRowV2 title="Account" description="Local mode — no sign-in required.">
              <div class="flex items-center gap-2">
                <Tag>Local</Tag>
              </div>
            </SettingsRowV2>

            <SettingsRowV2 title="License" description="Plan: local (full access, no subscription).">
              <div class="flex items-center gap-2">
                <Tag>Enabled</Tag>
              </div>
            </SettingsRowV2>
          </SettingsListV2>
        </div>
      </div>
    </>
  )
}
