// Copying text to the clipboard. Call `copyText` in the click handler itself (not after an await),
// so both ways keep the click's user activation: the Clipboard API (secure contexts: https and
// localhost), then `document.execCommand("copy")` through a temporary textarea (plain http and most
// frames). It resolves false when neither copied; the caller then shows the text to copy by hand.

/** Copy `text` through a temporary, selected textarea; false if the browser refused. */
function copyThroughSelection(text: string): boolean {
  const field = document.createElement("textarea")
  field.value = text
  field.readOnly = true
  field.setAttribute("aria-hidden", "true")
  field.style.position = "fixed"
  field.style.top = "0"
  field.style.left = "0"
  field.style.opacity = "0"
  field.style.pointerEvents = "none"
  const focused =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
  document.body.append(field)
  field.select()
  let copied = false
  try {
    // Deprecated, but the only way to copy where the Clipboard API is missing (plain http).
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    copied = document.execCommand("copy")
  } catch {
    copied = false
  } finally {
    field.remove()
    focused?.focus({ preventScroll: true })
  }
  return copied
}

/** Copy `text`: true once it is on the clipboard, false if the browser refused both ways. */
export async function copyText(text: string): Promise<boolean> {
  // Undefined outside a secure context, whatever the types say.
  const clipboard = navigator.clipboard as Clipboard | undefined
  if (typeof clipboard?.writeText === "function") {
    try {
      await clipboard.writeText(text)
      return true
    } catch {
      // Refused (no permission, no focus, a frame without clipboard-write): try the selection.
    }
  }
  return copyThroughSelection(text)
}
