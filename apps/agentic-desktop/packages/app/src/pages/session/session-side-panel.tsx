import { For, Match, Show, Switch, createEffect, createMemo, createResource, createSignal, onCleanup, type JSX } from "solid-js"
import { createStore } from "solid-js/store"
import { createMediaQuery } from "@solid-primitives/media"
import { Tabs } from "@opencode-ai/ui/tabs"
import { IconButton } from "@opencode-ai/ui/icon-button"
import { Icon } from "@opencode-ai/ui/icon"
import { TooltipKeybind } from "@opencode-ai/ui/tooltip"
import { ResizeHandle } from "@opencode-ai/ui/resize-handle"
import { Mark } from "@opencode-ai/ui/logo"
import { DragDropProvider, DragDropSensors, DragOverlay, SortableProvider, closestCenter } from "@thisbeyond/solid-dnd"
import type { DragEvent } from "@thisbeyond/solid-dnd"
import type { SnapshotFileDiff, VcsFileDiff } from "@opencode-ai/sdk/v2"
import { ConstrainDragYAxis, getDraggableId } from "@/utils/solid-dnd"
import { useDialog } from "@opencode-ai/ui/context/dialog"

import FileTree from "@/components/file-tree"
import { SessionContextUsage } from "@/components/session-context-usage"
import { SessionContextTab, SortableTab, FileVisual, PDKCatalogDock, DRCLVSDashboard, SchematicExplorer, SchematicTabContent, pathFromSchematicTab, SessionWaveformTab } from "@/components/session"
import { useCommand } from "@/context/command"
import { useFile, type SelectedLineRange } from "@/context/file"
import { useLanguage } from "@/context/language"
import { useLayout } from "@/context/layout"
import { usePlatform } from "@/context/platform"
import { useSettings } from "@/context/settings"
import { useSync } from "@/context/sync"
import { createFileTabListSync } from "@/pages/session/file-tab-scroll"
import { FileTabContent } from "@/pages/session/file-tabs"
import { createOpenSessionFileTab, createSessionTabs, getTabReorderIndex, type Sizing } from "@/pages/session/helpers"
import { setSessionHandoff } from "@/pages/session/handoff"
import { useSessionLayout } from "@/pages/session/session-layout"
import { callAgenticTool } from "@/utils/agentic"
import { decode64 } from "@/utils/base64"

type RenderDiff = (SnapshotFileDiff & { file: string }) | VcsFileDiff

function renderDiff(value: SnapshotFileDiff | VcsFileDiff): value is RenderDiff {
  return typeof value.file === "string"
}

/**
 * WaveformPanel — scans the real workspace for .vcd / .fst waveform files.
 * Shows an honest empty state if none exist. Never shows fake data.
 */
function WaveformPanel(props: { openTab: (tab: string) => void; file: any }) {
  const { params } = useSessionLayout()

  const [vcdFiles] = createResource(
    () => params.id,
    async (sessionId) => {
      if (!sessionId) return []
      try {
        const res = await callAgenticTool(
          "workspace",
          { session_id: sessionId, workspace_root: decode64(params.dir) ?? "" },
          { action: "list", pattern: "**/*" }
        )
        const files: string[] = res.result
          ? res.result.split("\n").map((f) => f.trim()).filter(Boolean)
          : []
        return files.filter((f) => f.endsWith(".vcd") || f.endsWith(".fst"))
      } catch {
        return []
      }
    },
  )

  const files = () => vcdFiles() ?? []

  return (
    <div class="flex flex-col w-full h-full bg-background-stronger font-sans">
      <div class="flex items-center justify-between px-3 py-1.5 border-b border-border-weaker-base text-12-medium text-text-strong shrink-0">
        <span>Waveform Viewer</span>
        <Show when={!vcdFiles.loading}>
          <span class="text-11-regular text-text-weaker">{files().length} file{files().length !== 1 ? "s" : ""}</span>
        </Show>
      </div>

      <Show when={vcdFiles.loading}>
        <div class="flex-1 flex items-center justify-center text-12-regular text-text-weak">
          Scanning workspace…
        </div>
      </Show>

      <Show when={!vcdFiles.loading && files().length === 0}>
        <div class="flex-1 flex flex-col items-center justify-center gap-2 px-4 text-center pb-16">
          <div class="text-12-medium text-text-weak">No waveform files found</div>
          <div class="text-11-regular text-text-weaker max-w-[180px]">
            Run a simulation to generate a .vcd file, then come back here.
          </div>
        </div>
      </Show>

      <Show when={!vcdFiles.loading && files().length > 0}>
        <div class="flex-1 overflow-y-auto">
          <For each={files()}>
            {(f) => (
              <div
                onClick={() => props.openTab(props.file.tab(f))}
                class="flex items-center gap-2 px-3 py-1.5 text-12-regular text-text-base hover:text-text-strong hover:bg-surface-base cursor-pointer border-b border-border-weaker-base last:border-b-0"
              >
                <span class="font-mono truncate">{f}</span>
              </div>
            )}
          </For>
        </div>
      </Show>
    </div>
  )
}


export function SessionSidePanel(props: {
  canReview: () => boolean
  diffs: () => (SnapshotFileDiff | VcsFileDiff)[]
  diffsReady: () => boolean
  empty: () => string
  hasReview: () => boolean
  reviewCount: () => number
  reviewPanel: () => JSX.Element
  activeDiff?: string
  focusReviewDiff: (path: string) => void
  reviewSnap: boolean
  size: Sizing
}) {
  const layout = useLayout()
  const platform = usePlatform()
  const settings = useSettings()
  const sync = useSync()
  const file = useFile()
  const language = useLanguage()
  const command = useCommand()
  const dialog = useDialog()
  const { sessionKey, tabs, view, params } = useSessionLayout()

  const isDesktop = createMediaQuery("(min-width: 768px)")
  const desktopV2 = () => platform.platform === "desktop" && settings.general.newLayoutDesigns()
  const shown = createMemo(() => (desktopV2() ? settings.general.showFileTree() : true))

  const reviewOpen = createMemo(() => isDesktop() && view().reviewPanel.opened())
  const fileOpen = createMemo(() => isDesktop() && shown() && layout.fileTree.opened())
  const open = createMemo(() => reviewOpen() || fileOpen())
  const reviewTab = createMemo(() => isDesktop())
  const panelWidth = createMemo(() => {
    if (!open()) return "0px"
    if (reviewOpen()) return "auto"
    return `${layout.fileTree.width()}px`
  })
  const treeWidth = createMemo(() => (fileOpen() ? `${layout.fileTree.width()}px` : "0px"))

  const diffs = createMemo(() => props.diffs().filter(renderDiff))
  const diffFiles = createMemo(() => diffs().map((d) => d.file))
  const kinds = createMemo(() => {
    const merge = (a: "add" | "del" | "mix" | undefined, b: "add" | "del" | "mix") => {
      if (!a) return b
      if (a === b) return a
      return "mix" as const
    }

    const normalize = (p: string) => p.replaceAll("\\\\", "/").replace(/\/+$/, "")

    const out = new Map<string, "add" | "del" | "mix">()
    for (const diff of diffs()) {
      const file = normalize(diff.file)
      const kind = diff.status === "added" ? "add" : diff.status === "deleted" ? "del" : "mix"

      out.set(file, kind)

      const parts = file.split("/")
      for (const [idx] of parts.slice(0, -1).entries()) {
        const dir = parts.slice(0, idx + 1).join("/")
        if (!dir) continue
        out.set(dir, merge(out.get(dir), kind))
      }
    }
    return out
  })

  const empty = (msg: string) => (
    <div class="h-full flex flex-col">
      <div class="h-6 shrink-0" aria-hidden />
      <div class="flex-1 pb-64 flex items-center justify-center text-center">
        <div class="text-12-regular text-text-weak">{msg}</div>
      </div>
    </div>
  )

  const nofiles = createMemo(() => {
    const state = file.tree.state("")
    if (!state?.loaded) return false
    return file.tree.children("").length === 0
  })

  const normalizeTab = (tab: string) => {
    if (!tab.startsWith("file://")) return tab
    return file.tab(tab)
  }

  const pathFromTab = (tab: string): string | undefined => {
    if (tab.startsWith("schematic:")) return pathFromSchematicTab(tab)
    return file.pathFromTab(tab)
  }

  const openReviewPanel = () => {
    if (!view().reviewPanel.opened()) view().reviewPanel.open()
  }

  const openTab = createOpenSessionFileTab({
    normalizeTab,
    openTab: tabs().open,
    pathFromTab: file.pathFromTab,
    loadFile: file.load,
    openReviewPanel,
    setActive: tabs().setActive,
  })

  const tabState = createSessionTabs({
    tabs,
    pathFromTab,
    normalizeTab,
    review: reviewTab,
    hasReview: props.canReview,
  })
  const contextOpen = tabState.contextOpen
  const openedTabs = tabState.openedTabs
  const activeTab = tabState.activeTab
  const activeFileTab = tabState.activeFileTab

  const fileTreeTab = () => layout.fileTree.tab()

  const setFileTreeTabValue = (value: string) => {
    if (value !== "changes" && value !== "all" && value !== "pdk" && value !== "signoff" && value !== "schematic" && value !== "waves") return
    layout.fileTree.setTab(value)
  }

  const showAllFiles = () => {
    if (fileTreeTab() !== "changes") return
    layout.fileTree.setTab("all")
  }

  const [store, setStore] = createStore({
    activeDraggable: undefined as string | undefined,
  })

  const handleDragStart = (event: unknown) => {
    const id = getDraggableId(event)
    if (!id) return
    setStore("activeDraggable", id)
  }

  const handleDragOver = (event: DragEvent) => {
    const { draggable, droppable } = event
    if (!draggable || !droppable) return

    const currentTabs = tabs().all()
    const toIndex = getTabReorderIndex(currentTabs, draggable.id.toString(), droppable.id.toString())
    if (toIndex === undefined) return
    tabs().move(draggable.id.toString(), toIndex)
  }

  const handleDragEnd = () => {
    setStore("activeDraggable", undefined)
  }

  createEffect(() => {
    if (!file.ready()) return

    setSessionHandoff(sessionKey(), {
      files: tabs()
        .all()
        .reduce<Record<string, SelectedLineRange | null>>((acc, tab) => {
          const path = file.pathFromTab(tab)
          if (!path) return acc

          const selected = file.selectedLines(path)
          acc[path] =
            selected && typeof selected === "object" && "start" in selected && "end" in selected
              ? (selected as SelectedLineRange)
              : null

          return acc
        }, {}),
    })
  })

  return (
    <Show when={isDesktop() && !(settings.general.newLayoutDesigns() && !params.id)}>
      <aside
        id="review-panel"
        aria-label={language.t("session.panel.reviewAndFiles")}
        aria-hidden={!open()}
        inert={!open()}
        class="relative min-w-0 h-full flex shrink-0 overflow-hidden bg-background-base"
        classList={{
          "pointer-events-none": !open(),
          "transition-[width] duration-[240ms] ease-[cubic-bezier(0.22,1,0.36,1)] will-change-[width] motion-reduce:transition-none":
            !props.size.active() && !props.reviewSnap,
          "flex-1": reviewOpen(),
        }}
        style={{ width: panelWidth() }}
      >
        <Show when={open()}>
          <div
            class="size-full flex border-l border-border-weaker-base"
          >
            <div
              aria-hidden={!reviewOpen()}
              inert={!reviewOpen()}
              class="relative min-w-0 h-full flex-1 overflow-hidden bg-background-base"
              classList={{
                "pointer-events-none": !reviewOpen(),
              }}
            >
              <div class="size-full min-w-0 h-full bg-background-base">
                <DragDropProvider
                  onDragStart={handleDragStart}
                  onDragEnd={handleDragEnd}
                  onDragOver={handleDragOver}
                  collisionDetector={closestCenter}
                >
                  <DragDropSensors />
                  <ConstrainDragYAxis />
                  <Tabs value={activeTab()} onChange={openTab}>
                    <div class="sticky top-0 shrink-0 flex">
                      <Tabs.List
                        ref={(el: HTMLDivElement) => {
                          const stop = createFileTabListSync({ el, contextOpen })
                          onCleanup(stop)
                        }}
                      >
                        <Show when={reviewTab() && props.canReview()}>
                          <Tabs.Trigger value="review">
                            <div class="flex items-center gap-1.5">
                              <div>{language.t("session.tab.review")}</div>
                              <Show when={props.hasReview()}>
                                <div>{props.reviewCount()}</div>
                              </Show>
                            </div>
                          </Tabs.Trigger>
                        </Show>
                        <Show when={contextOpen()}>
                          <Tabs.Trigger
                            value="context"
                            closeButton={
                              <TooltipKeybind
                                title={language.t("common.closeTab")}
                                keybind={command.keybind("tab.close")}
                                placement="bottom"
                                gutter={10}
                              >
                                <IconButton
                                  icon="close-small"
                                  variant="ghost"
                                  class="h-5 w-5"
                                  onClick={() => tabs().close("context")}
                                  aria-label={language.t("common.closeTab")}
                                />
                              </TooltipKeybind>
                            }
                            hideCloseButton
                            onMiddleClick={() => tabs().close("context")}
                          >
                            <div class="flex items-center gap-2">
                              <SessionContextUsage variant="indicator" />
                              <div>{language.t("session.tab.context")}</div>
                            </div>
                          </Tabs.Trigger>
                        </Show>
                        <SortableProvider ids={openedTabs()}>
                          <For each={openedTabs()}>{(tab) => <SortableTab tab={tab} onTabClose={tabs().close} />}</For>
                        </SortableProvider>
                        <div class="bg-background-stronger h-full shrink-0 sticky right-0 z-10 flex items-center justify-center pr-3">
                          <TooltipKeybind
                            title={language.t("command.file.open")}
                            keybind={command.keybind("file.open")}
                            class="flex items-center"
                          >
                            <IconButton
                              icon="plus-small"
                              variant="ghost"
                              iconSize="large"
                              class="!rounded-md"
                              onClick={() => {
                                void import("@/components/dialog-select-file").then((x) => {
                                  dialog.show(() => <x.DialogSelectFile mode="files" onOpenFile={showAllFiles} />)
                                })
                              }}
                              aria-label={language.t("command.file.open")}
                            />
                          </TooltipKeybind>
                        </div>
                      </Tabs.List>
                    </div>

                    <Show when={reviewTab() && props.canReview()}>
                      <Tabs.Content value="review" class="flex flex-col h-full overflow-hidden contain-strict">
                        <Show when={reviewOpen() && activeTab() === "review"}>{props.reviewPanel()}</Show>
                      </Tabs.Content>
                    </Show>

                    <Tabs.Content value="empty" class="flex flex-col h-full overflow-hidden contain-strict">
                      <Show when={activeTab() === "empty"}>
                        <div class="relative pt-2 flex-1 min-h-0 overflow-hidden">
                          <div class="h-full px-6 pb-42 -mt-4 flex flex-col items-center justify-center text-center gap-6">
                            <Mark class="w-14 opacity-10" />
                            <div class="text-14-regular text-text-weak max-w-56">
                              {language.t("session.files.selectToOpen")}
                            </div>
                          </div>
                        </div>
                      </Show>
                    </Tabs.Content>

                    <Show when={contextOpen()}>
                      <Tabs.Content value="context" class="flex flex-col h-full overflow-hidden contain-strict">
                        <Show when={activeTab() === "context"}>
                          <div class="relative pt-2 flex-1 min-h-0 overflow-hidden">
                            <SessionContextTab />
                          </div>
                        </Show>
                      </Tabs.Content>
                    </Show>

                    <Show when={activeFileTab()} keyed>
                      {(tab) => (
                        <Show
                          when={tab.startsWith("schematic:")}
                          fallback={<FileTabContent tab={tab} />}
                        >
                          <SchematicTabContent tab={tab} />
                        </Show>
                      )}
                    </Show>
                  </Tabs>
                  <DragOverlay>
                    <Show when={store.activeDraggable} keyed>
                      {(tab) => {
                        const path = file.pathFromTab(tab)
                        return (
                          <div data-component="tabs-drag-preview">
                            <Show when={path}>{(p) => <FileVisual active path={p()} />}</Show>
                          </div>
                        )
                      }}
                    </Show>
                  </DragOverlay>
                </DragDropProvider>
              </div>
            </div>

            <Show when={shown()}>
              <div
                id="file-tree-panel"
                aria-hidden={!fileOpen()}
                inert={!fileOpen()}
                class="relative min-w-0 h-full shrink-0 overflow-hidden"
                classList={{
                  "pointer-events-none": !fileOpen(),
                  "transition-[width] duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] will-change-[width] motion-reduce:transition-none":
                    !props.size.active(),
                }}
                style={{ width: treeWidth() }}
              >
                <div
                  class="h-full flex flex-col overflow-hidden group/filetree"
                  classList={{ "border-l border-border-weaker-base": reviewOpen() }}
                >
                  <div class="h-full flex flex-row">
                    <div class="w-12 shrink-0 flex flex-col items-center gap-1 py-2 border-r border-border-weaker-base bg-background-base">
                      <button
                        onClick={() => setFileTreeTabValue("changes")}
                        title={`${props.reviewCount()} ${language.t(props.reviewCount() === 1 ? "session.review.change.one" : "session.review.change.other")}`}
                        class="relative w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "changes",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "changes",
                        }}
                      >
                        <Icon name="branch" size="small" />
                        <Show when={props.reviewCount() > 0}>
                          <span class="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 inline-flex items-center justify-center rounded-full text-10-medium font-mono bg-surface-interactive-base text-text-on-interactive-base">
                            {props.reviewCount()}
                          </span>
                        </Show>
                      </button>
                      <button
                        onClick={() => setFileTreeTabValue("all")}
                        title={language.t("session.files.all")}
                        class="w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "all",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "all",
                        }}
                      >
                        <Icon name="file-tree" size="small" />
                      </button>
                      <button
                        onClick={() => setFileTreeTabValue("pdk")}
                        title="PDK"
                        class="w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "pdk",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "pdk",
                        }}
                      >
                        <Icon name="providers" size="small" />
                      </button>
                      <button
                        onClick={() => setFileTreeTabValue("signoff")}
                        title="Signoff"
                        class="w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "signoff",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "signoff",
                        }}
                      >
                        <Icon name="shield" size="small" />
                      </button>
                      <button
                        onClick={() => setFileTreeTabValue("schematic")}
                        title="RTL Schematic"
                        class="w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "schematic",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "schematic",
                        }}
                      >
                        <Icon name="code-lines" size="small" />
                      </button>
                      <button
                        onClick={() => setFileTreeTabValue("waves")}
                        title="Waves"
                        class="w-9 h-9 flex items-center justify-center rounded-md transition-colors"
                        classList={{
                          "bg-surface-base text-text-strong": fileTreeTab() === "waves",
                          "text-text-weaker hover:text-text-base hover:bg-surface-base": fileTreeTab() !== "waves",
                        }}
                      >
                        <Icon name="sliders" size="small" />
                      </button>
                    </div>
                    <div class="flex-1 min-w-0 overflow-y-auto overflow-x-hidden bg-background-stronger">
                      <Switch>
                        <Match when={fileTreeTab() === "changes"}>
                          <Switch>
                            <Match when={props.hasReview() || !props.diffsReady()}>
                              <Show
                                when={props.diffsReady()}
                                fallback={
                                  <div class="px-2 py-2 text-12-regular text-text-weak">
                                    {language.t("common.loading")}
                                    {language.t("common.loading.ellipsis")}
                                  </div>
                                }
                              >
                                <FileTree
                                  path=""
                                  class="pt-3 px-3"
                                  allowed={diffFiles()}
                                  kinds={kinds()}
                                  draggable={false}
                                  active={props.activeDiff}
                                  onFileClick={(node) => props.focusReviewDiff(node.path)}
                                />
                              </Show>
                            </Match>
                          </Switch>
                        </Match>
                        <Match when={fileTreeTab() === "all"}>
                          <Switch>
                            <Match when={nofiles()}>{empty(language.t("session.files.empty"))}</Match>
                            <Match when={true}>
                              <FileTree
                                path=""
                                class="pt-3 px-3"
                                modified={diffFiles()}
                                kinds={kinds()}
                                onFileClick={(node) => openTab(file.tab(node.path))}
                              />
                            </Match>
                          </Switch>
                        </Match>
                        <Match when={fileTreeTab() === "pdk"}>
                          <div class="h-full contain-strict">
                            <PDKCatalogDock />
                          </div>
                        </Match>
                        <Match when={fileTreeTab() === "signoff"}>
                          <div class="h-full contain-strict">
                            <DRCLVSDashboard />
                          </div>
                        </Match>
                        <Match when={fileTreeTab() === "schematic"}>
                          <div class="h-full contain-strict overflow-y-auto">
                            <SchematicExplorer />
                          </div>
                        </Match>
                        <Match when={fileTreeTab() === "waves"}>
                          <div class="h-full contain-strict">
                            <WaveformPanel openTab={openTab} file={file} />
                          </div>
                        </Match>
                      </Switch>
                    </div>
                  </div>
                </div>
                <Show when={fileOpen()}>
                  <div onPointerDown={() => props.size.start()}>
                    <ResizeHandle
                      direction="horizontal"
                      edge="start"
                      size={layout.fileTree.width()}
                      min={200}
                      max={480}
                      onResize={(width) => {
                        props.size.touch()
                        layout.fileTree.resize(width)
                      }}
                    />
                  </div>
                </Show>
              </div>
            </Show>
          </div>
        </Show>
      </aside>
    </Show>
  )
}
