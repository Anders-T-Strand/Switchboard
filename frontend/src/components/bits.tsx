import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useApp } from "../hooks";

export function Panel({ title, sub, actions, children, className = "" }: {
  title?: string;
  sub?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel-head">
          <div>
            {title && <h2>{title}</h2>}
            {sub && <p className="sub">{sub}</p>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export type Readout = { label: string; value: ReactNode; detail?: ReactNode };

export function Readouts({ items }: { items: Readout[] }) {
  return (
    <dl className="readouts">
      {items.map((it) => (
        <div className="readout" key={it.label}>
          <dt>{it.label}</dt>
          <dd className="readout-value">{it.value}</dd>
          {it.detail && <dd className="readout-detail">{it.detail}</dd>}
        </div>
      ))}
    </dl>
  );
}

export function ModelTag({ modelKey }: { modelKey: string | null | undefined }) {
  const { colors, label } = useApp();
  if (!modelKey) return <span className="muted">–</span>;
  return (
    <span className="model-tag">
      <span className="swatch" style={{ background: colors[modelKey] ?? "#7d7266" }} />
      {label(modelKey)}
    </span>
  );
}

export function Md({ children }: { children: string }) {
  return (
    <div className="md">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" /> }}
      >
        {children}
      </Markdown>
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div className="error-note" role="alert">
      {children}
    </div>
  );
}

export function Light({ on, warn, children }: { on: boolean; warn?: boolean; children: ReactNode }) {
  return (
    <span className="light">
      <span className={`lamp ${on ? (warn ? "warn" : "on") : "off"}`} aria-hidden />
      {children}
    </span>
  );
}
