import { TOO_FEW, TOO_FEW_EXPLAINED, TOO_FEW_WORDS } from "./applications";
import { Tooltip } from "../ui/Tooltip";

/**
 * A figure's text; the dash a figure with too few requests behind it is drawn as carries its
 * meaning in a tooltip and, for screen readers, in words (docs/54).
 */
export function FigureText({ text }: { text: string }) {
  if (text !== TOO_FEW) return <>{text}</>;
  return (
    <Tooltip text={TOO_FEW_EXPLAINED} className="too-few">
      <span aria-hidden="true">{TOO_FEW}</span>
      <span className="visually-hidden">{TOO_FEW_WORDS}</span>
    </Tooltip>
  );
}
