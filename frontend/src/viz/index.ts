// The 2D renderers: the scatter and pole figure, the heatmap with its overlay layer, the
// interactive colorbar and its colormaps, and the linked selection shared by the views; the 3D
// view, whose VTK.js loads with import() on its first mount; and the unit-cell view (Canvas2D).
export {
  createSelectionStore,
  EMPTY_SELECTION,
  linkedSelection,
  selectionKey,
  selectionMask,
  selectionPairs,
  useLinkedSelection,
  type LinkedSelection,
  type SelectionPair,
  type SelectionStore,
} from "./linked-selection"
export {
  PoleFigureView,
  ScatterView,
  type PoleFigureViewProps,
  type ScatterViewProps,
} from "./scatter/ScatterView"
export {
  PointCloud3DView,
  type PointCloud3DViewProps,
} from "./cloud/PointCloud3DView"
export type { PointCloudAxes, PointCloudData } from "./cloud/types"
export {
  NON_INDEXED_COLORS,
  type MarkerShape,
  type NonIndexedColor,
  type ScatterColors,
} from "./scatter/stamp"
export type {
  ScatterData,
  ScatterSelection,
  ScatterTool,
} from "./scatter/types"
export {
  COLORMAP_LABELS,
  colormapPixels,
  colormapRgb,
  FRAME_COLORMAPS,
  MAP_PALETTES,
  type ColormapName,
  type FrameColormap,
  type MapPalette,
} from "./color/colormaps"
export {
  computeDistribution,
  useDistribution,
  type DistributionState,
} from "./color/compute"
export {
  distributionOf,
  quantile,
  type Distribution,
  type DistributionInput,
} from "./color/distribution"
export { scalarLookup } from "./color/lut"
export {
  SCALE_KINDS,
  SCALE_LABELS,
  type ColorScale,
  type ScaleKind,
} from "./color/scale"
export { ColorBar, type ColorBarProps } from "./colorbar/ColorBar"
export {
  AUTO_RANGES,
  colorbarSettings,
  colorScaleOf,
  FRAME_AUTO_RANGE,
  MAP_AUTO_RANGE,
  readColorbarSettings,
  resolveRange,
  type AutoRangeId,
  type ColorbarSettings,
  type Range,
} from "./colorbar/logic"
export { decodeFrame, type Frame, type FrameData } from "./heatmap/frame"
export { HeatmapView, type HeatmapViewProps } from "./heatmap/HeatmapView"
export {
  hklLabel,
  OVERLAY_STYLES,
  overlayLayers,
  type OverlayBox,
  type OverlayKind,
  type OverlayLayer,
  type OverlayOptions,
} from "./heatmap/overlays"
export { UnitCellView, type UnitCellViewProps } from "./cell/UnitCellView"
export {
  crystalDirection,
  DEFAULT_CAMERA,
  dualBasis,
  latticeParameters,
  mat3Of,
  nearestSetting,
  rotationFromReference,
  type Camera,
  type LatticeParameters,
  type Mat3,
  type Rotation,
  type Vec3,
} from "./cell/geometry"
