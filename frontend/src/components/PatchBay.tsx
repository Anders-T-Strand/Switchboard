import { useEffect, useMemo, useRef, useState } from "react";
import type { Summary } from "../api";
import { capitalize, int } from "../format";
import { useApp } from "../hooks";

type Props = {
  byTaskType: Summary["by_task_type"];
  modelKeys: string[];
  loadedKeys: string[];
};

type Cord = { task: string; model: string; n: number; d: string; width: number };

const W = 880;
const ROW = 46;
const PAD_Y = 30;
const LEFT_X = 190;
const RIGHT_X = W - 250;

/**
 * Prompts drawn as patch cords: task-type jacks on the left, model jacks on the
 * right, one cord per (task type, model) pair, thickness by prompt count.
 */
export function PatchBay({ byTaskType, modelKeys, loadedKeys }: Props) {
  const { colors, label } = useApp();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [focus, setFocus] = useState<{ task?: string; model?: string } | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; cord: Cord } | null>(null);
  const [asTable, setAsTable] = useState(false);
  const [narrow, setNarrow] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < 640));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout = useMemo(() => {
    const tasks = byTaskType.map((t) => t.task_type);
    const modelTotals: Record<string, number> = {};
    byTaskType.forEach((t) => Object.entries(t.models).forEach(([m, n]) => (modelTotals[m] = (modelTotals[m] ?? 0) + n)));
    const models = [...modelKeys, ...Object.keys(modelTotals).filter((m) => !modelKeys.includes(m))];
    const rows = Math.max(tasks.length, models.length, 3);
    const height = PAD_Y * 2 + (rows - 1) * ROW;
    const yFor = (i: number, count: number) => PAD_Y + ((rows - count) * ROW) / 2 + i * ROW;
    const taskY: Record<string, number> = {};
    tasks.forEach((t, i) => (taskY[t] = yFor(i, tasks.length)));
    const modelY: Record<string, number> = {};
    models.forEach((m, i) => (modelY[m] = yFor(i, models.length)));

    const maxN = Math.max(1, ...byTaskType.flatMap((t) => Object.values(t.models)));
    const cords: Cord[] = [];
    for (const t of byTaskType) {
      for (const [m, n] of Object.entries(t.models)) {
        if (!n || modelY[m] === undefined) continue;
        const y1 = taskY[t.task_type];
        const y2 = modelY[m];
        const x1 = LEFT_X + 10;
        const x2 = RIGHT_X - 10;
        const sag = 28 + Math.abs(y2 - y1) * 0.14;
        const d = `M${x1},${y1} C${x1 + (x2 - x1) * 0.32},${y1 + sag} ${x1 + (x2 - x1) * 0.68},${y2 + sag} ${x2},${y2}`;
        cords.push({ task: t.task_type, model: m, n, d, width: 2 + 9 * Math.sqrt(n / maxN) });
      }
    }
    cords.sort((a, b) => b.width - a.width);
    return { tasks, models, modelTotals, taskY, modelY, cords, height };
  }, [byTaskType, modelKeys]);

  const total = byTaskType.reduce((s, t) => s + t.total, 0);
  const isLit = (c: Cord) =>
    !focus || ((!focus.task || focus.task === c.task) && (!focus.model || focus.model === c.model));

  const moveTip = (e: React.MouseEvent, cord: Cord) => {
    const box = wrapRef.current?.getBoundingClientRect();
    if (!box) return;
    setTip({ x: e.clientX - box.left, y: e.clientY - box.top, cord });
  };

  const summaryText =
    total === 0
      ? "No auto-routed prompts in this period."
      : layout.cords.map((c) => `${c.task} to ${label(c.model)}: ${c.n}`).join(", ");

  return (
    <div className="patchbay" ref={wrapRef}>
      <div className="patchbay-head">
        <div>
          <h2>Where prompts went</h2>
          <p className="sub">
            {total === 0 ? "Auto-routed prompts, by task type and the model that answered" : `${int(total)} auto-routed prompts, by task type and the model that answered`}
          </p>
        </div>
        <button className="ghost small" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
          {asTable ? "Show cords" : "Show table"}
        </button>
      </div>

      {asTable ? (
        <table className="data compact">
          <thead>
            <tr>
              <th>Task type</th>
              {layout.models.map((m) => (
                <th key={m} className="num">{label(m)}</th>
              ))}
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {byTaskType.map((t) => (
              <tr key={t.task_type}>
                <td>{capitalize(t.task_type)}</td>
                {layout.models.map((m) => (
                  <td key={m} className="num">{t.models[m] ? int(t.models[m]) : "–"}</td>
                ))}
                <td className="num">{int(t.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : narrow ? (
        <NarrowBay byTaskType={byTaskType} models={layout.models} modelTotals={layout.modelTotals} />
      ) : (
        <svg
          viewBox={`0 0 ${W} ${layout.height}`}
          role="img"
          aria-label={summaryText}
          onMouseLeave={() => {
            setFocus(null);
            setTip(null);
          }}
        >
          <g className="cords">
            {layout.cords.map((c) => (
              <path
                key={`${c.task}|${c.model}`}
                d={c.d}
                stroke={colors[c.model] ?? "#7d7266"}
                strokeWidth={c.width}
                className={isLit(c) ? "cord" : "cord dim"}
                onMouseEnter={(e) => {
                  setFocus({ task: c.task, model: c.model });
                  moveTip(e, c);
                }}
                onMouseMove={(e) => moveTip(e, c)}
                onMouseLeave={() => {
                  setFocus(null);
                  setTip(null);
                }}
              />
            ))}
          </g>

          {layout.tasks.map((t) => {
            const y = layout.taskY[t];
            const n = byTaskType.find((x) => x.task_type === t)?.total ?? 0;
            return (
              <g
                key={t}
                className="jack-row"
                onMouseEnter={() => setFocus({ task: t })}
                onMouseLeave={() => setFocus(null)}
              >
                <text x={LEFT_X - 22} y={y - 2} textAnchor="end" className="jack-label">{capitalize(t)}</text>
                <text x={LEFT_X - 22} y={y + 15} textAnchor="end" className="jack-count">
                  {int(n)} {n === 1 ? "prompt" : "prompts"}
                </text>
                <Jack x={LEFT_X} y={y} live />
              </g>
            );
          })}

          {layout.models.map((m) => {
            const y = layout.modelY[m];
            const n = layout.modelTotals[m] ?? 0;
            const loaded = loadedKeys.includes(m);
            return (
              <g
                key={m}
                className="jack-row"
                onMouseEnter={() => setFocus({ model: m })}
                onMouseLeave={() => setFocus(null)}
              >
                <Jack x={RIGHT_X} y={y} live={n > 0} color={colors[m]} />
                <text x={RIGHT_X + 22} y={y - 2} className={n > 0 ? "jack-label" : "jack-label idle"}>{label(m)}</text>
                <text x={RIGHT_X + 22} y={y + 15} className="jack-count">
                  {n > 0 ? `${int(n)} ${n === 1 ? "prompt" : "prompts"}` : "idle"}
                  {loaded ? ", in VRAM" : ""}
                </text>
              </g>
            );
          })}

          {total === 0 && (
            <text x={(LEFT_X + RIGHT_X) / 2} y={layout.height / 2} textAnchor="middle" className="patch-empty">
              No routes yet. Send a prompt from the Console.
            </text>
          )}
        </svg>
      )}

      {tip && !asTable && (
        <div className="tooltip" style={{ left: tip.x + 14, top: tip.y + 14 }}>
          <div className="tip-title">
            {capitalize(tip.cord.task)} to {label(tip.cord.model)}
          </div>
          <div>
            {int(tip.cord.n)} {tip.cord.n === 1 ? "prompt" : "prompts"}
          </div>
        </div>
      )}
    </div>
  );
}

/** Phone-width version: one segmented bar per task type, split by model. */
function NarrowBay({ byTaskType, models, modelTotals }: {
  byTaskType: Summary["by_task_type"];
  models: string[];
  modelTotals: Record<string, number>;
}) {
  const { colors, label } = useApp();
  if (byTaskType.length === 0) return <p className="patch-empty-text">No routes yet. Send a prompt from the Console.</p>;
  const used = models.filter((m) => modelTotals[m]);
  return (
    <>
      <ul className="patch-list">
        {byTaskType.map((t) => {
          const parts = used.filter((m) => t.models[m]);
          return (
            <li key={t.task_type}>
              <div className="patch-row-head">
                <span className="jack-label-html">{capitalize(t.task_type)}</span>
                <span className="muted">{int(t.total)} {t.total === 1 ? "prompt" : "prompts"}</span>
              </div>
              <div
                className="segbar"
                role="img"
                aria-label={parts.map((m) => `${label(m)}: ${t.models[m]}`).join(", ")}
              >
                {parts.map((m) => (
                  <span key={m} style={{ flexGrow: t.models[m], background: colors[m] ?? "#7d7266" }} />
                ))}
              </div>
            </li>
          );
        })}
      </ul>
      <ul className="legend patch-legend">
        {used.map((m) => (
          <li key={m}>
            <span className="swatch" style={{ background: colors[m] ?? "#7d7266" }} />
            {label(m)} {int(modelTotals[m])}
          </li>
        ))}
      </ul>
    </>
  );
}

function Jack({ x, y, live, color }: { x: number; y: number; live: boolean; color?: string }) {
  return (
    <g className={live ? "jack" : "jack idle"}>
      <circle cx={x} cy={y} r={11} className="jack-plate" />
      <circle cx={x} cy={y} r={7} className="jack-hole" />
      {color && live && <circle cx={x} cy={y} r={3} fill={color} />}
    </g>
  );
}
