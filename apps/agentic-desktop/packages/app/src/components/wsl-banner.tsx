import { createResource, createSignal, Show, onMount } from "solid-js"
import { Icon } from "@opencode-ai/ui/icon"
import { usePlatform } from "@/context/platform"
import { useServer } from "@/context/server"
import { useWslServers } from "@/wsl/context"
import { useDialog } from "@opencode-ai/ui/context/dialog"
import { Dialog } from "@opencode-ai/ui/v2/dialog-v2"
import { DialogAddWslServer } from "@/wsl/dialog-add-server"
import { useLanguage } from "@/context/language"
import { showToast } from "@/utils/toast"

export function WslBanner() {
  const platform = usePlatform()
  const server = useServer()
  const wslServers = useWslServers()
  const dialog = useDialog()
  const language = useLanguage()

  const [dismissed, setDismissed] = createSignal(localStorage.getItem("wsl_banner_dismissed") === "true")
  const [installing, setInstalling] = createSignal(false)
  const [installResult, setInstallResult] = createSignal<string | null>(null)
  const [dockerAvailable, setDockerAvailable] = createSignal<boolean | null>(null)

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const api = window.api as any | undefined

  const [mode] = createResource(async () => {
    if (platform.platform !== "desktop") return "unknown"
    if (platform.os !== "windows") return "unknown"
    try {
      return await api?.getBackendMode?.()
    } catch {
      return "unknown"
    }
  })

  const isWslConnection = () => server.current?.type === "sidecar" && server.current.variant === "wsl"
  const isWslAvailable = () => !!wslServers.data?.runtime?.available

  const show = () =>
    !dismissed() &&
    mode() === "windows-native" &&
    !isWslConnection() &&
    platform.platform === "desktop" &&
    platform.os === "windows"

  const handleInstallWsl = async () => {
    setInstalling(true)
    setInstallResult(null)
    try {
      const result = await api?.installWsl?.()
      if (result) {
        if (result.message.includes("ERROR_ALREADY_EXISTS") || result.message.toLowerCase().includes("already exists")) {
          setInstallResult("WSL is already installed! Please link your WSL distro to run AgentIC natively inside Linux.")
        } else {
          setInstallResult(result.message)
        }
      }
    } catch {
      setInstallResult("Failed to start WSL installer. Open an admin terminal and run: wsl --install")
    }
    setInstalling(false)
  }

  const handleOpenAddWsl = () => {
    dialog.push(() => (
      <Dialog title={language.t("wsl.server.add")} size="large" fit class="settings-v2-wsl-dialog">
        <DialogAddWslServer />
      </Dialog>
    ))
  }

  const handleCheckDocker = async () => {
    try {
      const result = await api?.checkDocker?.()
      const available = result?.available ?? false
      setDockerAvailable(available)
      if (available) {
        localStorage.setItem("wsl_banner_dismissed", "true")
        setDismissed(true)
        showToast({
          variant: "success",
          title: "Docker mode active",
          description: "Docker Desktop detected! Tool executions will run inside Docker containers where available.",
        })
      }
    } catch {
      setDockerAvailable(false)
    }
  }

  const handleDismiss = () => {
    localStorage.setItem("wsl_banner_dismissed", "true")
    setDismissed(true)
  }

  return (
    <Show when={show()}>
      <div class="shrink-0 border-b border-border-weaker-base bg-surface-warning-weak">
        <div class="flex items-start gap-3 px-4 py-3">
          <Icon name="warning" class="text-text-strong shrink-0 mt-0.5" />

          <div class="flex-1 min-w-0">
            <div class="text-13-medium text-text-strong mb-1">
              WSL not detected — EDA tools unavailable
            </div>
            <div class="text-12-regular text-text-base leading-relaxed mb-2.5">
              AgentIC needs a Linux environment to run EDA tools (Yosys, OpenROAD, Magic, KLayout).
              Without WSL, you can chat and write code but cannot synthesize, simulate, or do physical design.
            </div>

            <Show when={installResult()}>
              <div class="text-11-regular text-text-strong bg-surface-base rounded-md px-3 py-2 mb-2.5 font-mono">
                {installResult()}
              </div>
            </Show>

            <Show when={dockerAvailable() === false}>
              <div class="text-11-regular text-text-weaker mb-2.5">
                Docker not found. Install{" "}
                <a
                  href="https://docs.docker.com/desktop/install/windows-install/"
                  class="text-text-interactive-base underline"
                  onClick={(e) => e.preventDefault()}
                >
                  Docker Desktop
                </a>
                , then relaunch AgentIC.
              </div>
            </Show>

            <div class="flex items-center gap-2">
              <Show
                when={isWslAvailable()}
                fallback={
                  <button
                    class="inline-flex items-center gap-1.5 px-3 py-1.5 text-12-medium bg-surface-base hover:bg-surface-stronger text-text-strong rounded-md border border-border-weaker-base transition-colors disabled:opacity-50"
                    onClick={handleInstallWsl}
                    disabled={installing()}
                  >
                    <Show when={installing()} fallback={<span>Install WSL</span>}>
                      <span>Installing...</span>
                    </Show>
                  </button>
                }
              >
                <button
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-12-medium bg-surface-base hover:bg-surface-stronger text-text-strong rounded-md border border-border-weaker-base transition-colors"
                  onClick={handleOpenAddWsl}
                >
                  <span>Link WSL Distro</span>
                </button>
              </Show>
              <button
                class="inline-flex items-center gap-1.5 px-3 py-1.5 text-12-medium bg-transparent hover:bg-surface-base text-text-base rounded-md border border-border-weaker-base transition-colors"
                onClick={handleCheckDocker}
              >
                <Show when={dockerAvailable() === null} fallback={
                  <Show when={dockerAvailable()} fallback={<span>Docker not found</span>}>
                    <span>Docker detected — relaunch to use</span>
                  </Show>
                }>
                  <span>Use Docker</span>
                </Show>
              </button>
              <button
                class="inline-flex items-center px-2 py-1.5 text-12-regular text-text-weaker hover:text-text-base transition-colors"
                onClick={handleDismiss}
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      </div>
    </Show>
  )
}

