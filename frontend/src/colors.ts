// Series colors. Validated as a set (dataviz six-check validator) against the dark
// panel surface #26201b: lightness band, chroma, adjacent CVD and normal-vision
// separation, and 3:1 contrast all pass. Slots are assigned in this fixed order,
// never cycled, and a color always follows its model, not its rank.
export const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
export const OVERFLOW = "#7d7266";

export type ColorMap = Record<string, string>;

/**
 * Router model keys take slots in config order. Claude Code model families
 * (opus, sonnet...) reuse the color of the router model with the same name, so
 * "Claude Opus" is the same color in every chart. Families with no router
 * counterpart take the next free slot.
 */
export function buildColorMap(modelKeys: string[], families: string[]): ColorMap {
  const map: ColorMap = {};
  let slot = 0;
  for (const key of modelKeys) {
    map[key] = slot < SERIES.length ? SERIES[slot] : OVERFLOW;
    slot++;
  }
  for (const fam of families) {
    const twin = modelKeys.find((k) => k.toLowerCase().includes(fam.toLowerCase()));
    if (twin) {
      map[`cc:${fam}`] = map[twin];
    } else {
      map[`cc:${fam}`] = slot < SERIES.length ? SERIES[slot] : OVERFLOW;
      slot++;
    }
  }
  return map;
}
