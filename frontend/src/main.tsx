import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App";
import { client } from "./api/generated/client.gen";
import "./shared/tokens.css";

client.setConfig({ baseUrl: window.location.origin });

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { networkMode: "always", retry: false },
    mutations: { networkMode: "always", retry: false },
  },
});

const root = document.getElementById("root");

if (root === null) {
  throw new Error("Pensae Signal root element was not found");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
