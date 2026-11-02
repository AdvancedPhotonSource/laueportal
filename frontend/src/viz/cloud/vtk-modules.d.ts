// Declarations for the VTK.js modules the 3D view uses that ship without typings: the OpenGL
// implementations (imported for their side effect, registering them with the view-node factory)
// and the cube axes actor (only the members the scene calls).

declare module "@kitware/vtk.js/Rendering/OpenGL/Actor"
declare module "@kitware/vtk.js/Rendering/OpenGL/Camera"
declare module "@kitware/vtk.js/Rendering/OpenGL/CubeAxesActor"
declare module "@kitware/vtk.js/Rendering/OpenGL/PolyDataMapper"
declare module "@kitware/vtk.js/Rendering/OpenGL/Renderer"
declare module "@kitware/vtk.js/Rendering/OpenGL/Texture"

declare module "@kitware/vtk.js/Rendering/Core/CubeAxesActor" {
  import type vtkActor from "@kitware/vtk.js/Rendering/Core/Actor"
  import type vtkCamera from "@kitware/vtk.js/Rendering/Core/Camera"

  interface TextStyle {
    fontColor?: string
    fontStyle?: string
    fontSize?: number
    fontFamily?: string
  }

  export interface vtkCubeAxesActor extends vtkActor {
    setCamera(camera: vtkCamera | null): void
    setDataBounds(bounds: number[]): boolean
    setAxisLabels(labels: string[]): boolean
    setAxisTextStyle(style: TextStyle): void
    setTickTextStyle(style: TextStyle): void
    setAxisTitlePixelOffset(px: number): boolean
    setTickLabelPixelOffset(px: number): boolean
  }

  const vtkCubeAxesActor: {
    newInstance(initialValues?: object): vtkCubeAxesActor
  }
  export default vtkCubeAxesActor
}
