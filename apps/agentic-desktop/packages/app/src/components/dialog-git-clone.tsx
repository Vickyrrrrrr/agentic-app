import { Show } from "solid-js"
import { Button } from "@opencode-ai/ui/button"
import { TextField } from "@opencode-ai/ui/text-field"
import { useMutation } from "@tanstack/solid-query"
import { createStore } from "solid-js/store"
import { showToast } from "@/utils/toast"

export function DialogGitClone(props: {
  open: boolean
  onClose: () => void
}) {
  const [store, setStore] = createStore({ url: "", token: "" })

  const cloneMutation = useMutation(() => ({
    mutationFn: async () => {
      const url = store.url.trim()
      if (!url) throw new Error("Enter a GitHub repository URL")
      const agenticUrl = localStorage.getItem("agentic_local_api_base")?.replace(/\/+$/, "") || "http://127.0.0.1:7860"
      const res = await fetch(`${agenticUrl}/opencode/git/clone`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: "git_clone",
          args: { url, token: store.token.trim() || undefined },
          session_id: "ui",
          workspace_root: "",
        }),
      })
      const data = await res.json()
      if (!data.success) throw new Error(data.result || "Clone failed")
      return data.result
    },
    onSuccess: (result) => {
      showToast({ title: "Cloned", description: result })
      setStore("url", "")
      setStore("token", "")
      props.onClose()
    },
    onError: (err: Error) => {
      showToast({ title: "Clone failed", description: err.message })
    },
  }))

  function handleSubmit(e: SubmitEvent) {
    e.preventDefault()
    if (cloneMutation.isPending) return
    cloneMutation.mutate()
  }

  return (
    <Show when={props.open}>
      <div
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        onClick={(e) => { if (e.target === e.currentTarget) props.onClose() }}
      >
        <div class="bg-surface-panel rounded-xl border border-border-weak-base shadow-xl w-full max-w-[420px] mx-4">
          <div class="flex items-center justify-between px-5 pt-4 pb-2">
            <span class="text-14-medium text-text-strong">Clone GitHub Repository</span>
            <button
              type="button"
              class="titlebar-icon w-7 h-6 p-0 box-border shrink-0"
              onClick={props.onClose}
              aria-label="Close"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
            </button>
          </div>
          <form onSubmit={handleSubmit} class="flex flex-col gap-3 px-5 pb-5 pt-1">
            <TextField
              autofocus
              type="text"
              label="Repository URL"
              placeholder="https://github.com/user/repo"
              value={store.url}
              onChange={(v) => setStore("url", v)}
            />
            <TextField
              type="password"
              label="Token (optional, for private repos)"
              placeholder="ghp_..."
              value={store.token}
              onChange={(v) => setStore("token", v)}
            />
            <div class="flex justify-end gap-2 mt-1">
              <Button type="button" variant="ghost" size="large" onClick={props.onClose}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" size="large" disabled={cloneMutation.isPending}>
                {cloneMutation.isPending ? "Cloning..." : "Clone"}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </Show>
  )
}
