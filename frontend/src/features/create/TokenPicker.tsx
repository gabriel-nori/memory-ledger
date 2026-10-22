import { useMemo, useState } from "react";
import { Badge, Button, SearchInput, Icon } from "@/components";
import "./TokenPicker.css";

export interface TokenOption {
  value: string;
  label: string;
}

export interface TokenPickerProps {
  options: TokenOption[];
  value: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  emptyLabel: string;
  extraAction?: { label: string; onClick: () => void };
}

/**
 * Pick several values from a searchable list of cards.
 *
 * Cards rather than a `<select multiple>`: the things being picked are skills,
 * rules and node types, and a bare token asks the person to already recognise
 * it. A card can carry what it is.
 */
export function TokenPicker({
  options,
  value,
  onChange,
  placeholder,
  emptyLabel,
  extraAction,
}: TokenPickerProps) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");

  const labels = useMemo(
    () => new Map(options.map((o) => [o.value, o.label])),
    [options],
  );

  const shown = useMemo(() => {
    const q = term.trim().toLowerCase();
    const pool = options.filter((o) => !value.includes(o.value));
    return q ? pool.filter((o) => `${o.value} ${o.label}`.toLowerCase().includes(q)) : pool.slice(0, 40);
  }, [options, value, term]);

  return (
    <div className="ml-tokens">
      <div className="ml-tokens__chips">
        {value.length ? (
          value.map((v) => (
            <span className="ml-tokens__chip" key={v}>
              {labels.get(v) ?? v}
              <button
                type="button"
                aria-label={`remove ${labels.get(v) ?? v}`}
                onClick={() => onChange(value.filter((x) => x !== v))}
              >
                <Icon name="close" />
              </button>
            </span>
          ))
        ) : (
          <Badge>{emptyLabel}</Badge>
        )}
      </div>

      <div className="ml-tokens__bar">
        <Button size="sm" onClick={() => setOpen((v) => !v)} active={open}>
          ＋ {placeholder}
        </Button>
        {extraAction ? (
          <Button size="sm" onClick={extraAction.onClick}>{extraAction.label}</Button>
        ) : null}
      </div>

      {open ? (
        <div className="ml-tokens__panel">
          <SearchInput value={term} onChange={setTerm} placeholder="search…" />
          <div className="ml-tokens__list">
            {shown.length ? (
              shown.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  className="ml-tokens__opt"
                  onClick={() => {
                    onChange([...value, o.value]);
                    setTerm("");
                  }}
                >
                  <b>{o.label}</b>
                  <span>{o.value}</span>
                </button>
              ))
            ) : (
              <p className="ml-tokens__none">nothing matches</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
