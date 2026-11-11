"use client";

export interface IndicatorOptions {
  sma50: boolean;
  sma200: boolean;
  rsi: boolean;
}
export default function TechnicalIndicators({
  value,
  onChange,
  disabled = false,
}: {
  value: IndicatorOptions;
  onChange: (next: IndicatorOptions) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset className="indicator-controls" disabled={disabled}>
      <legend className="sr-only">Technical indicators</legend>
      {(
        [
          ["sma50", "SMA 50"],
          ["sma200", "SMA 200"],
          ["rsi", "RSI 14"],
        ] as const
      ).map(([key, label]) => (
        <label key={key} className={`indicator-toggle indicator-${key}`}>
          <input
            type="checkbox"
            checked={value[key]}
            onChange={(event) =>
              onChange({ ...value, [key]: event.target.checked })
            }
          />
          <span className="indicator-swatch" />
          {label}
        </label>
      ))}
    </fieldset>
  );
}
