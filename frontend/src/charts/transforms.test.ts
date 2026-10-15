import { simulated } from "../test/fixtures";
import {
  bucketSize,
  downsample,
  formatAge,
  formatUsd,
  formatValue,
  largest,
  secondsBefore,
  toColumns,
  toPercent,
  yScale,
  type Columns,
} from "./transforms";

interface P {
  t_ms: number;
  a: number | null;
  b: number | null;
}

const pick = [(p: P) => p.a, (p: P) => p.b];

describe("toColumns", () => {
  it("puts each picked field in its own column", () => {
    const points: P[] = [
      { t_ms: 0, a: 1, b: 10 },
      { t_ms: 1000, a: 2, b: null },
      { t_ms: 2000, a: 3, b: 30 },
    ];
    expect(toColumns(points, pick, 1000)).toEqual({
      xs: [0, 1000, 2000],
      ys: [
        [1, 2, 3],
        [10, null, 30],
      ],
    });
  });

  it("fills the seconds the API left out with nulls, so a line breaks there", () => {
    const points: P[] = [
      { t_ms: 5000, a: 1, b: 1 },
      { t_ms: 8000, a: 4, b: 4 },
      { t_ms: 9000, a: 5, b: 5 },
    ];
    expect(toColumns(points, pick, 1000)).toEqual({
      xs: [5000, 6000, 7000, 8000, 9000],
      ys: [
        [1, null, null, 4, 5],
        [1, null, null, 4, 5],
      ],
    });
  });

  it("follows the step it is given", () => {
    const points: P[] = [
      { t_ms: 0, a: 1, b: 1 },
      { t_ms: 1500, a: 2, b: 2 },
    ];
    expect(toColumns(points, pick, 500).xs).toEqual([0, 500, 1000, 1500]);
  });

  it("gives empty columns, one per line, for no points", () => {
    expect(toColumns([], pick, 1000)).toEqual({ xs: [], ys: [[], []] });
  });

  it("lays out the gateway's own series, gap included", () => {
    // Premium had points at 0, 3000, 4000, 5000 and 6000 ms: nothing at 1000 or 2000.
    const points = simulated.models.premium?.series ?? [];
    const columns = toColumns(points, [(p) => p.total_p99_ms, (p) => p.batch_size_max], 1000);
    expect(columns.xs).toEqual([0, 1000, 2000, 3000, 4000, 5000, 6000]);
    expect(columns.ys[0]).toEqual([null, null, null, 855.5, null, 2910, 3732.5]);
    // Nobody looked at the batch in the first second, so that is a gap too, not a zero.
    expect(columns.ys[1]).toEqual([null, null, null, 8, 8, 8, 2]);
  });
});

describe("downsample", () => {
  const columns: Columns = {
    xs: [0, 1, 2, 3, 4, 5, 6],
    ys: [
      [1, 9, 2, null, null, 4, 3],
      [1, 9, 2, null, null, 4, 3],
      [1, 9, 2, null, null, 4, 3],
      [1, 9, 2, null, null, 4, 3],
    ],
  };

  it("returns the same columns when they already fit", () => {
    expect(downsample(columns, 7, ["max"])).toBe(columns);
    expect(downsample(columns, 100, ["max"])).toBe(columns);
  });

  it("combines consecutive rows with each line's own reducer", () => {
    // Seven rows into at most four points: two rows per point.
    const result = downsample(columns, 4, ["max", "min", "mean", "last"]);
    expect(result.xs).toEqual([0, 2, 4, 6]);
    expect(result.ys[0]).toEqual([9, 2, 4, 3]);
    expect(result.ys[1]).toEqual([1, 2, 4, 3]);
    expect(result.ys[2]).toEqual([5, 2, 4, 3]);
    expect(result.ys[3]).toEqual([9, 2, 4, 3]);
  });

  it("keeps a gap only where every combined row was a gap", () => {
    const result = downsample({ xs: [0, 1, 2, 3], ys: [[null, null, null, 5]] }, 2, ["max"]);
    expect(result).toEqual({ xs: [0, 2], ys: [[null, 5]] });
  });

  it("keeps a spike that plain decimation would drop", () => {
    const ys = Array.from({ length: 900 }, (_, i) => (i === 457 ? 5000 : 20));
    const result = downsample({ xs: ys.map((_, i) => i * 1000), ys: [ys] }, 300, ["max"]);
    expect(result.xs).toHaveLength(300);
    expect(result.xs[1]).toBe(3000);
    expect(largest(result.ys)).toBe(5000);
    expect(result.ys[0]?.[152]).toBe(5000);
  });

  it("never draws more than the limit, whatever the length", () => {
    for (const rows of [1, 299, 300, 301, 899, 900, 901, 5000]) {
      const xs = Array.from({ length: rows }, (_, i) => i);
      const result = downsample({ xs, ys: [xs] }, 300, ["last"]);
      expect(result.xs.length).toBeLessThanOrEqual(300);
      expect(result.ys[0]?.at(-1)).toBe(rows - 1);
    }
  });

  it("sizes buckets by rounding up", () => {
    expect(bucketSize(900, 300)).toBe(3);
    expect(bucketSize(901, 300)).toBe(4);
    expect(bucketSize(10, 300)).toBe(1);
    expect(bucketSize(0, 300)).toBe(1);
    expect(bucketSize(10, 0)).toBe(10);
  });
});

describe("time", () => {
  it("turns clock times into seconds before now", () => {
    expect(secondsBefore([4000, 5000, 6000], 6500)).toEqual([-2.5, -1.5, -0.5]);
  });

  it("formats an age as minutes and seconds", () => {
    expect(formatAge(0)).toBe("now");
    expect(formatAge(-0.4)).toBe("now");
    expect(formatAge(-5)).toBe("-0:05");
    expect(formatAge(-75)).toBe("-1:15");
    expect(formatAge(-900)).toBe("-15:00");
  });
});

describe("yScale", () => {
  it("fits the data when there is nothing else", () => {
    expect(yScale(200, null, null)).toEqual({ max: 220.00000000000003, referenceShown: false });
    expect(yScale(null, null, null)).toEqual({ max: 1, referenceShown: false });
  });

  it("stretches to a reference line that is close to the data", () => {
    const scale = yScale(1500, 2000, null);
    expect(scale.referenceShown).toBe(true);
    expect(scale.max).toBeCloseTo(2200);
  });

  it("stays on the data when it is already over the reference line", () => {
    const scale = yScale(3732.5, 2000, null);
    expect(scale.referenceShown).toBe(true);
    expect(scale.max).toBeCloseTo(4105.75);
  });

  it("leaves out a reference line far above the data and says so", () => {
    // A 30 s SLO over 40 ms latencies would flatten the lines into the axis.
    const scale = yScale(40, 30_000, null);
    expect(scale.referenceShown).toBe(false);
    expect(scale.max).toBeCloseTo(44);
  });

  it("shows the reference line when there is no data yet", () => {
    expect(yScale(null, 2000, null).referenceShown).toBe(true);
  });

  it("runs to a fixed maximum, such as a capacity", () => {
    expect(yScale(3, 8, 8)).toEqual({ max: 8.4, referenceShown: true });
    expect(yScale(null, null, 100)).toEqual({ max: 105, referenceShown: false });
  });
});

describe("formatting", () => {
  it("formats values for reading", () => {
    expect(formatValue(null)).toBe("–");
    expect(formatValue(0)).toBe("0");
    expect(formatValue(7.2000000000000455)).toBe("7.2");
    expect(formatValue(97.5)).toBe("97.5");
    expect(formatValue(3732.5)).toBe("3,733");
    expect(formatValue(100000000)).toBe("100,000,000");
  });

  it("formats money, with more digits for very small amounts", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(12.5)).toBe("$12.50");
    expect(formatUsd(0.0009273600000000002)).toBe("$0.000927");
  });

  it("turns a ratio into a percentage and keeps a gap a gap", () => {
    expect(toPercent(0.25)).toBe(25);
    expect(toPercent(null)).toBeNull();
  });

  it("finds the largest value across lines", () => {
    expect(
      largest([
        [1, null, 7],
        [null, 3],
      ]),
    ).toBe(7);
    expect(largest([[null], []])).toBeNull();
  });
});
