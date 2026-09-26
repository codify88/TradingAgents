import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./components/Shell";
import { Toaster } from "./components/toast";
import { Empty } from "./components/ui";
import { BookScreen } from "./screens/Book";
import { PlanDetailScreen } from "./screens/PlanDetail";
import { SecurityScreen } from "./screens/Security";
import { TodayScreen } from "./screens/Today";
import "./styles.css";

const root = createRootRoute();
const shell = createRoute({ getParentRoute: () => root, id: "shell", component: Shell });
const routes = [
  createRoute({ getParentRoute: () => shell, path: "/", component: TodayScreen }),
  createRoute({ getParentRoute: () => shell, path: "/plan/$id", component: PlanDetailScreen }),
  createRoute({ getParentRoute: () => shell, path: "/book", component: BookScreen }),
  createRoute({ getParentRoute: () => shell, path: "/security", component: SecurityScreen }),
];
const router = createRouter({
  routeTree: root.addChildren([shell.addChildren(routes)]),
  defaultNotFoundComponent: () => <Empty title="Nothing here">That page doesn't exist in Desk.</Empty>,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const queries = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 10_000 } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queries}>
      <Toaster>
        <RouterProvider router={router} />
      </Toaster>
    </QueryClientProvider>
  </StrictMode>,
);

// Installable on the phone; the worker caches only hashed assets (public/sw.js).
if ("serviceWorker" in navigator && import.meta.env.PROD && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    /* the app works without it */
  });
}
