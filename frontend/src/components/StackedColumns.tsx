import { useEffect, useRef, useState } from "react";
import type { Row } from "../api";
import { shortDate } from "../format";

type Props = {
  rows: Row[];
  keys: string[];
  colorFor: (key: string) => string;
  labelFor: (key: string) => string;
  format: (v: number) => string;
  formatAxis?: (v: number) => string;
  emptyText: string;
  height?: number;
  xLabel?: (row: Row) => string;
};

const M = { top: 10, right: 6, bottom: 26, left: 50 };

function niceScale(max: number): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s * 4 >= max) ?? 10 * pow;
  return [0, step, step * 2, step * 3, step * 4];
}

function roundedTop(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.min(r, h, w / 2);
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
}

/** Stacked column chart: one column per row, segments stacked in `keys` order. */
export function StackedColumns({
  rows,
  keys,
  colorFor,
  labelFor,
  format,
  formatAxis,
  emptyText,
  height = 220,
  xLabel = (r) => shortDate(String(r.date)),
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(260, entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const val = (r: Row, k: string) => Number(r[k] ?? 0) || 0;
  const totals = rows.map((r) => keys.reduce((s, k) => s + val(r, k), 0));
  const empty = totals.every((t) => t === 0);
  const ticks = niceScale(Math.max(...totals, 0));
  const top = ticks[ticks.length - 1];
  const plotW = width - M.left - M.right;
  const plotH = height - M.top - M.bottom;
  const band = plotW / Math.max(rows.length, 1);
  const barW = Math.min(24, band * 0.62);
  const stride = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(plotW / 58))));
  const yOf = (v: number) => M.top + plotH - (v / top) * plotH;
  const axisFmt = formatAxis ?? format;

  return (
    <div className="chart" ref={ref}>
      <div className="chart-bar">
        {keys.length > 1 ? (
          <ul className="legend">
            {keys.map((k) => (
              <li key={k}>
                <span className="swatch" style={{ background: colorFor(k) }} />
                {labelFor(k)}
              </li>
            ))}
          </ul>
        ) : (
          <span />
        )}
        {!empty && (
          <button className="ghost small" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
            {asTable ? "Show chart" : "Show table"}
          </button>
        )}
      </div>

      {empty ? (
        <div className="chart-empty" style={{ height }}>{emptyText}</div>
      ) : asTable ? (
        <div className="table-scroll" style={{ maxHeight: height + 40 }}>
          <table className="data compact">
            <thead>
              <tr>
                <th>Day</th>
                {keys.map((k) => (
                  <th key={k} className="num">{labelFor(k)}</th>
                ))}
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={String(r.date)}>
                  <td>{xLabel(r)}</td>
                  {keys.map((k) => (
                    <td key={k} className="num">{format(val(r, k))}</td>
                  ))}
                  <td className="num">{format(totals[i])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-plot">
          <svg width={width} height={height} role="img" aria-label={`Chart with ${rows.length} columns`}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={M.left} x2={width - M.right} y1={yOf(t)} y2={yOf(t)} className={t === 0 ? "axis" : "grid"} />
                <text x={M.left - 8} y={yOf(t) + 4} textAnchor="end" className="tick">{axisFmt(t)}</text>
              </g>
            ))}

            {rows.map((r, i) => {
              const cx = M.left + band * (i + 0.5);
              const x = cx - barW / 2;
              let base = yOf(0);
              const segs = keys
                .map((k) => ({ k, v: val(r, k) }))
                .filter((s) => s.v > 0);
              return (
                <g key={String(r.date)}>
                  {hover === i && (
                    <rect x={cx - band / 2} y={M.top} width={band} height={plotH} className="col-wash" />
                  )}
                  {segs.map((s, j) => {
                    const h = (s.v / top) * plotH;
                    const y = base - h;
                    base = y;
                    const drawH = j === 0 ? h : Math.max(h - 2, 0.75);
                    const isTop = j === segs.length - 1;
                    return isTop ? (
                      <path key={s.k} d={roundedTop(x, y, barW, drawH, 4)} fill={colorFor(s.k)} />
                    ) : (
                      <rect key={s.k} x={x} y={y} width={barW} height={drawH} fill={colorFor(s.k)} />
                    );
                  })}
                  {i % stride === 0 && (
                    <text x={cx} y={height - 8} textAnchor="middle" className="tick">{xLabel(r)}</text>
                  )}
                  <rect
                    x={cx - band / 2}
                    y={M.top}
                    width={band}
                    height={plotH}
                    fill="transparent"
                    onMouseEnter={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                  />
                </g>
              );
            })}
          </svg>

          {hover !== null && (
            <div
              className="tooltip"
              style={
                // Sit beside the hovered column, on whichever side has room.
                M.left + band * (hover + 0.5) > width / 2
                  ? { right: width - (M.left + band * hover) + 6, top: M.top }
                  : { left: M.left + band * (hover + 1) + 6, top: M.top }
              }
            >
              <div className="tip-title">{xLabel(rows[hover])}</div>
              {keys
                .filter((k) => val(rows[hover], k) > 0)
                .map((k) => (
                  <div className="tip-row" key={k}>
                    <span className="swatch" style={{ background: colorFor(k) }} />
                    <span>{labelFor(k)}</span>
                    <span className="num">{format(val(rows[hover], k))}</span>
                  </div>
                ))}
              {keys.length > 1 && (
                <div className="tip-row total">
                  <span />
                  <span>Total</span>
                  <span className="num">{format(totals[hover])}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
