import type { ScanDetail } from "@/api"
import { DefinitionList } from "@/components/DefinitionList"
import { metadataSections } from "./scan-detail"
import { ScanCard } from "./ScanCard"

/**
 * The Metadata tab: the scan log's record, read-only, in sections (the ID taper shows the
 * taper's own values), each scan row in its own section.
 */
export function ScanMetadataTab({ scan }: { scan: ScanDetail }) {
  return (
    <div className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
      {metadataSections(scan.metadata, scan.dimensions).map((section) => (
        <ScanCard key={section.title} title={section.title}>
          {section.items.length > 0 ? (
            <DefinitionList rows={section.items} />
          ) : (
            <p className="text-muted-foreground">Not recorded.</p>
          )}
        </ScanCard>
      ))}
    </div>
  )
}
