import { RouterProvider } from "@tanstack/react-router"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { Providers } from "./app/Providers"
import { createQueryClient } from "./app/query-client"
import { createAppRouter } from "./app/router"
import "./index.css"

const queryClient = createQueryClient()
const router = createAppRouter(queryClient)

const root = document.getElementById("root")
if (!root) throw new Error("missing #root element")

createRoot(root).render(
  <StrictMode>
    <Providers queryClient={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  </StrictMode>
)
