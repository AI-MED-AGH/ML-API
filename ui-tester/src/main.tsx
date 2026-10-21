import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// theme before first paint; the toggle in App owns it afterwards
try {
  document.documentElement.classList.toggle("dark", localStorage.getItem("theme") !== "light");
} catch {
  document.documentElement.classList.add("dark");
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
