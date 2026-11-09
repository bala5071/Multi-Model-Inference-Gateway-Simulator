import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "../hooks/useTheme";
import { ConsoleApp } from "./ConsoleApp";
import "../styles.css";
import "./console.css";

const root = document.getElementById("root");
if (root === null) throw new Error("console.html has no #root element");
createRoot(root).render(
  <StrictMode>
    <ThemeProvider>
      <ConsoleApp />
    </ThemeProvider>
  </StrictMode>,
);
