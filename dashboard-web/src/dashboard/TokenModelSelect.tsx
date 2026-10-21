import React, { useEffect, useId, useRef, useState } from "react";

interface TokenModelSelectProps {
  options: Array<[string, string]>;
  selected: string[];
  onChange: (selected: string[]) => void;
}

/** Compact checkbox picker; selecting models never moves the surrounding charts. */
export function TokenModelSelect({ options, selected, onChange }: TokenModelSelectProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const selectedLabels = options.filter(([key]) => selected.includes(key)).map(([, label]) => label);
  const caption = selectedLabels.length === 0 ? "All models"
    : selectedLabels.length === 1 ? selectedLabels[0] : `${selectedLabels.length} models`;

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  return (
    <div
      ref={root}
      className="token-model-control"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus({ preventScroll: true });
        }
      }}
    >
      <span className="token-model-label">Models</span>
      <button
        ref={trigger}
        type="button"
        className="token-model-trigger"
        aria-label="Token models"
        aria-expanded={open}
        aria-controls={menuId}
        title={selectedLabels.length ? selectedLabels.join(", ") : "All models"}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{caption}</span><span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div id={menuId} className="token-model-menu" role="group" aria-label="Select token models">
          <button type="button" aria-pressed={selected.length === 0} onClick={() => onChange([])}>
            All models
          </button>
          {options.map(([key, label]) => (
            <label key={key}>
              <input
                type="checkbox"
                value={key}
                checked={selected.includes(key)}
                onChange={(event) => onChange(event.target.checked
                  ? [...selected, key] : selected.filter((item) => item !== key))}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}
