import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "./hooks/useTheme";
import { Shell } from "./shell/Shell";
import "./styles/tokens.css";
import "./styles.css";
import "./console/console.css";
import "./styles/frame.css";

const root = document.getElementById("root");
if (root === null) throw new Error("index.html has no #root element");
createRoot(root).render(
  <StrictMode>
    <ThemeProvider>
      <Shell />
    </ThemeProvider>
  </StrictMode>,
);
