import { showToast } from "./toast"

/**
 * Open a lazily-loaded dialog. A failed chunk load (corrupt install,
 * interrupted update) surfaces a toast instead of a dead click plus an
 * unhandled rejection.
 *
 * Literal English strings on purpose: this path must not depend on the
 * i18n system, which may live in the chunk that failed to load.
 */
export function showLazyDialog<T>(load: () => Promise<T>, open: (module: T) => void): void {
  void load().then(open, () => {
    showToast({
      title: "Couldn't open dialog",
      description: "A part of the app failed to load. Restart the app; if it keeps happening, reinstall it.",
    })
  })
}
