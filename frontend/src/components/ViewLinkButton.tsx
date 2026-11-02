import { LinkIcon } from "lucide-react"
import { useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { copyText } from "@/lib/clipboard"

/**
 * "Copy link to this view", for every viewer. `link` is the viewer's packed link
 * (src/lib/use-view-link.ts: packed ahead of the click, so the copy runs in the click itself;
 * `copyText`: the Clipboard API, then the selection). When the browser refuses both, a dialog
 * shows the link selected, for Ctrl+C.
 */
export function ViewLinkButton({
  link,
}: {
  link: () => string | Promise<string>
}) {
  const [packing, setPacking] = useState(false)
  const [shown, setShown] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)

  const copy = (url: string) =>
    copyText(url).then((copied) => {
      if (copied) toast.success("Link copied")
      else setShown(url)
    })

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="ml-auto"
        disabled={packing}
        onClick={() => {
          const url = link()
          if (typeof url === "string") {
            void copy(url)
            return
          }
          setPacking(true)
          void url.then(copy).finally(() => {
            setPacking(false)
          })
        }}
      >
        <LinkIcon aria-hidden />
        Copy link to this view
      </Button>
      <Dialog
        open={shown !== null}
        onOpenChange={(open) => {
          if (!open) setShown(null)
        }}
      >
        <DialogContent initialFocus={field} className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Link to this view</DialogTitle>
            <DialogDescription>Press Ctrl+C to copy it.</DialogDescription>
          </DialogHeader>
          <Input
            ref={field}
            readOnly
            aria-label="Link to this view"
            value={shown ?? ""}
            onFocus={(event) => {
              event.currentTarget.select()
            }}
          />
        </DialogContent>
      </Dialog>
    </>
  )
}
