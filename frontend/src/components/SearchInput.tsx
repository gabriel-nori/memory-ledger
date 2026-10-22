import { forwardRef } from "react";
import "./SearchInput.css";
import { Icon } from "./Icon";

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** Shown only while there is something to clear, so it never sits there dead. */
  clearLabel?: string;
  "aria-label"?: string;
  id?: string;
}

/** A search box with the clear button INSIDE it, so the field keeps its width. */
export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(function SearchInput(
  { value, onChange, placeholder, clearLabel = "clear search (Esc)", id, ...rest },
  ref,
) {
  return (
    <div className={`ml-search${value ? " has-value" : ""}`}>
      <input
        ref={ref}
        id={id}
        className="ml-search__input"
        type="search"
        value={value}
        placeholder={placeholder}
        aria-label={rest["aria-label"] ?? placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape" && value) {
            e.stopPropagation();
            onChange("");
          }
        }}
      />
      {value ? (
        <button type="button" className="ml-search__clear" title={clearLabel} aria-label={clearLabel} onClick={() => onChange("")}>
          <Icon name="close" />
        </button>
      ) : null}
    </div>
  );
});
