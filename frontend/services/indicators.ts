import type { Candle } from "@/services/api";
export interface IndicatorPoint {
  time: string;
  value: number;
}

export function simpleMovingAverage(
  candles: Candle[],
  period: number,
): IndicatorPoint[] {
  if (!Number.isInteger(period) || period < 1)
    throw new RangeError("Indicator period must be a positive integer");
  const points: IndicatorPoint[] = [];
  let sum = 0;
  for (let index = 0; index < candles.length; index++) {
    sum += candles[index].close;
    if (index >= period) sum -= candles[index - period].close;
    if (index >= period - 1)
      points.push({ time: candles[index].time, value: sum / period });
  }
  return points;
}

export function relativeStrengthIndex(
  candles: Candle[],
  period = 14,
): IndicatorPoint[] {
  if (!Number.isInteger(period) || period < 1)
    throw new RangeError("Indicator period must be a positive integer");
  const points: IndicatorPoint[] = [];
  if (candles.length <= period) return points;
  let gain = 0,
    loss = 0;
  for (let index = 1; index <= period; index++) {
    const delta = candles[index].close - candles[index - 1].close;
    gain += Math.max(delta, 0) / period;
    loss += Math.max(-delta, 0) / period;
  }
  function value() {
    return loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss);
  }
  points.push({ time: candles[period].time, value: value() });
  for (let index = period + 1; index < candles.length; index++) {
    const delta = candles[index].close - candles[index - 1].close;
    gain = (gain * (period - 1) + Math.max(delta, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-delta, 0)) / period;
    points.push({ time: candles[index].time, value: value() });
  }
  return points;
}
