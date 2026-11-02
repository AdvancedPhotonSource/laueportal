import { useEffect, useRef, useState } from "react"

import { useGetScan, type ScanDetail } from "@/api"
import { ErrorState } from "@/components/ErrorState"
import { LoadingState } from "@/components/LoadingState"
import { UnsavedChangesGuard } from "@/components/UnsavedChangesGuard"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useRetryFocus } from "@/lib/retry-focus"
import { isScanTab, SCAN_TABS, TAB_LABELS, type ScanTab } from "./scan-detail"
import { ScanCatalogForm, type CatalogFormHandle } from "./ScanCatalogForm"
import { ScanInfoTab } from "./ScanInfoTab"
import { ScanMetadataTab } from "./ScanMetadataTab"
import { ScanRunCards } from "./ScanRunCards"

/** The route's loader has just read the scan: the page does not read it again for this long. */
const SCAN_STALE_MS = 10_000

/**
 * A scan: the Scan Info, Metadata and Catalog tabs (the route keeps the tab in the URL), then the
 * scan's reconstructions (depth scans) and peak indexings.
 */
export function ScanDetailPage({
  scanId,
  tab,
  onTabChange,
}: {
  scanId: number
  tab: ScanTab
  onTabChange: (tab: ScanTab) => void
}) {
  const scan = useGetScan(scanId, { query: { staleTime: SCAN_STALE_MS } })
  // A Retry that loads takes its alert away: the heading gets the keyboard.
  const headingRef = useRef<HTMLHeadingElement>(null)
  const markRetry = useRetryFocus(
    scan.error !== null,
    scan.isFetching,
    headingRef
  )
  const retry = () => {
    markRetry()
    void scan.refetch({ cancelRefetch: false })
  }

  return (
    <section
      className="flex flex-col gap-4"
      aria-busy={scan.data === undefined && scan.error === null}
    >
      <h1
        ref={headingRef}
        tabIndex={-1}
        className="text-xl font-semibold outline-none"
      >
        Scan {scanId}
      </h1>
      {scan.error !== null && (
        <ErrorState
          error={scan.error}
          onRetry={retry}
          refreshing={scan.isFetching}
          stale={scan.data !== undefined}
        />
      )}
      {scan.data ? (
        <>
          <ScanTabs scan={scan.data} tab={tab} onTabChange={onTabChange} />
          <ScanRunCards scanId={scanId} technique={scan.data.technique} />
        </>
      ) : (
        scan.error === null && <LoadingState label="Loading the scan" />
      )}
    </section>
  )
}

function ScanTabs({
  scan,
  tab,
  onTabChange,
}: {
  scan: ScanDetail
  tab: ScanTab
  onTabChange: (tab: ScanTab) => void
}) {
  const scanId = scan.scan_number
  const catalogRef = useRef<CatalogFormHandle>(null)
  const [noteDirty, setNoteDirty] = useState(false)
  const [catalogDirty, setCatalogDirty] = useState(false)
  // "Fill in the catalog" hides itself with Scan Info: once the Catalog tab shows, its first field
  // takes the keyboard.
  const focusCatalogRef = useRef(false)
  useEffect(() => {
    if (tab !== "catalog" || !focusCatalogRef.current) return
    focusCatalogRef.current = false
    requestAnimationFrame(() => {
      catalogRef.current?.focusFirstField()
    })
  }, [tab])
  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        if (isScanTab(value)) onTabChange(value)
      }}
    >
      <TabsList variant="line">
        {SCAN_TABS.map((value) => (
          <TabsTrigger key={value} value={value}>
            {TAB_LABELS[value]}
          </TabsTrigger>
        ))}
      </TabsList>
      {/* Kept mounted: a note or catalog edit survives a look at another tab. */}
      <TabsContent value="info" keepMounted className="pt-2">
        <ScanInfoTab
          key={scanId}
          scan={scan}
          onFillCatalog={() => {
            focusCatalogRef.current = true
            onTabChange("catalog")
          }}
          onNoteAdded={(text, catalog) => {
            catalogRef.current?.noteAdded(text, catalog)
          }}
          onNoteDirtyChange={setNoteDirty}
        />
      </TabsContent>
      <TabsContent value="metadata" className="pt-2">
        <ScanMetadataTab scan={scan} />
      </TabsContent>
      <TabsContent value="catalog" keepMounted className="pt-2">
        <ScanCatalogForm
          key={scanId}
          ref={catalogRef}
          scanId={scanId}
          catalog={scan.catalog}
          onDirtyChange={setCatalogDirty}
        />
      </TabsContent>
      <UnsavedChangesGuard when={noteDirty || catalogDirty} />
    </Tabs>
  )
}
