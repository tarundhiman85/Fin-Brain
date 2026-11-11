import { test, expect } from "@playwright/test";
import {
  simpleMovingAverage,
  relativeStrengthIndex,
} from "../services/indicators";
const candles = (closes: number[]) =>
  closes.map((close, index) => ({
    time: `2025-01-${String(index + 1).padStart(2, "0")}`,
    open: close,
    high: close,
    low: close,
    close,
    volume: 10,
  }));

test("SMA excludes warmup and updates the rolling window", () => {
  expect(simpleMovingAverage(candles([1, 2, 3, 4]), 3)).toEqual([
    { time: "2025-01-03", value: 2 },
    { time: "2025-01-04", value: 3 },
  ]);
  expect(simpleMovingAverage(candles([1, 2]), 50)).toEqual([]);
});
test("Wilder RSI initializes correctly and uses recursive smoothing", () => {
  const values = relativeStrengthIndex(candles([1, 2, 3, 2, 4]), 3);
  expect(values[0].value).toBeCloseTo(66.6666667);
  expect(values[1].value).toBeCloseTo(83.3333333);
});
test("RSI handles rising, falling, flat and insufficient history", () => {
  expect(relativeStrengthIndex(candles([1, 2, 3, 4]), 3)[0].value).toBe(100);
  expect(relativeStrengthIndex(candles([4, 3, 2, 1]), 3)[0].value).toBe(0);
  expect(relativeStrengthIndex(candles([2, 2, 2, 2]), 3)[0].value).toBe(50);
  expect(relativeStrengthIndex(candles([1, 2]), 14)).toEqual([]);
});

test("indicator periods reject invalid inputs", () => {
  expect(() => simpleMovingAverage(candles([1, 2]), 0)).toThrow(RangeError);
  expect(() => relativeStrengthIndex(candles([1, 2]), 1.5)).toThrow(RangeError);
});
