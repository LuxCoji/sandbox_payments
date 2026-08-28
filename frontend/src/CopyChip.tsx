import { useState } from "react";

interface Props {
  value: string;
  display?: string;
  style?: React.CSSProperties;
}

/** Click-to-copy chip, reusing the existing .hash-chip look everywhere an id
 * needs to be copyable (checkpoint ids, branch ids, session ids). */
export default function CopyChip({ value, display, style }: Props) {
  const [copied, setCopied] = useState(false);

  async function copy(e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  return (
    <span
      className="hash-chip"
      style={{ cursor: "pointer", userSelect: "none", ...style }}
      onClick={copy}
      title={copied ? "Copied!" : `Click to copy: ${value}`}
    >
      {copied ? "✓ copied" : (display ?? value)}
    </span>
  );
}
