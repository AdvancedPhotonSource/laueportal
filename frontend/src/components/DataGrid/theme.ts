// AG Grid's theme bound to the app's theme tokens: every colour is a shadcn CSS variable, so the grid
// follows the light and dark themes with the rest of the UI. AG Grid's own mode switch, the
// `data-ag-theme-mode` attribute, is kept in step with the `.dark` class on <html> (applyTheme in
// src/lib/theme.ts and public/theme-init.js). Compact as the rest of the UI: 32 px rows.

import {
  ColumnApiModule,
  InfiniteRowModelModule,
  DateFilterModule,
  GridStateModule,
  NumberFilterModule,
  RowApiModule,
  RowSelectionModule,
  ScrollApiModule,
  TextFilterModule,
  ValidationModule,
  themeQuartz,
  type Module,
} from "ag-grid-community"

export const gridTheme = themeQuartz
  .withParams({
    fontFamily: "inherit",
    fontSize: 13,
    headerFontSize: 13,
    headerFontWeight: 600,
    spacing: 6,
    rowHeight: 32,
    headerHeight: 34,
    iconSize: 14,
    cellHorizontalPaddingScale: 0.7,
    borderRadius: "calc(var(--radius) * 0.6)",
    wrapperBorderRadius: "var(--radius)",
    browserColorScheme: "inherit",

    backgroundColor: "var(--background)",
    foregroundColor: "var(--foreground)",
    textColor: "var(--foreground)",
    subtleTextColor: "var(--muted-foreground)",
    borderColor: "var(--border)",
    // Text-on-background blue (the dark --primary is a fill): active filters, focus, radios.
    accentColor: "var(--link)",
    invalidColor: "var(--destructive)",
    chromeBackgroundColor: "var(--muted)",
    headerBackgroundColor: "var(--muted)",
    headerTextColor: "var(--foreground)",
    iconColor: "var(--muted-foreground)",
    rowHoverColor: "color-mix(in oklch, var(--muted) 70%, transparent)",
    selectedRowBackgroundColor:
      "color-mix(in oklch, var(--primary) 12%, transparent)",
    menuBackgroundColor: "var(--popover)",
    menuTextColor: "var(--popover-foreground)",
    menuBorder: { color: "var(--border)" },
    panelBackgroundColor: "var(--popover)",
    inputBackgroundColor: "var(--background)",
    inputBorder: { color: "var(--input)" },
    inputFocusBorder: { color: "var(--ring)" },
    // Opaque, as the app's focus rings (--ring is at least 3:1 against every surface; at 50% it was not).
    inputFocusShadow: "0 0 0 2px var(--ring)",
    inputPlaceholderTextColor: "var(--muted-foreground)",
    focusShadow: "0 0 0 2px var(--ring)",
    checkboxBorderRadius: 4,
    // An unchecked box's border: --input was 1.26:1 against the light background.
    checkboxUncheckedBorderColor: "var(--muted-foreground)",
    checkboxCheckedBackgroundColor: "var(--primary)",
    checkboxCheckedBorderColor: "var(--primary)",
    checkboxCheckedShapeColor: "var(--primary-foreground)",
    tooltipBackgroundColor: "var(--foreground)",
    tooltipTextColor: "var(--background)",
  })
  // The dark --primary is a deep blue: at 12% a selected row was 1.06:1 against the others.
  .withParams(
    {
      selectedRowBackgroundColor:
        "color-mix(in oklch, var(--primary) 55%, transparent)",
    },
    "dark"
  )

/**
 * The AG Grid modules the DataGrid uses, registered with each grid (only what is used). The
 * validation module explains misconfigurations in development builds only.
 */
export const GRID_MODULES: Module[] = [
  InfiniteRowModelModule,
  TextFilterModule,
  NumberFilterModule,
  DateFilterModule,
  RowSelectionModule,
  RowApiModule,
  GridStateModule,
  // The columns' widths and pinning, and the visible width: columns kept in view (pinning.ts).
  ColumnApiModule,
  ScrollApiModule,
  ...(import.meta.env.DEV ? [ValidationModule] : []),
]
