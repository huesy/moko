import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

// The bouncy, dismissable modal chrome shared by the feed's EventModal and the
// brain browser's RecordModal: backdrop, exit animation, Escape-to-close, and
// the header. Callers supply the body. Keeping this one shell means every modal
// in the monitor opens, closes, and animates identically.
export function ModalShell({
  icon,
  cls,
  title,
  chip,
  time,
  children,
  onClose,
}: {
  icon: string;
  cls: string; // accent class, e.g. "ev-thought"
  title: string;
  chip?: string;
  time?: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  const [closing, setClosing] = useState(false);

  // Play the exit animation, then unmount.
  const close = () => {
    setClosing(true);
    setTimeout(onClose, 200);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return createPortal(
    <div className={`modal-backdrop${closing ? " closing" : ""}`} onClick={close}>
      <div className={`modal ${cls}${closing ? " closing" : ""}`} onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <span className="modal-icon">{icon}</span>
          <div className="modal-headtext">
            <div className="modal-title">{title}</div>
            <div className="modal-sub">
              {chip && <span className="modal-chip">{chip}</span>}
              {time && <span className="modal-time">{time}</span>}
            </div>
          </div>
          <button className="modal-x" onClick={close} aria-label="Close">
            ✕
          </button>
        </header>
        {children}
      </div>
    </div>,
    document.body,
  );
}

// Shared field renderer for the modals' detail tables.
export function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}

export function fullTime(at?: string | null): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

// Time-of-day only (the feed's per-row stamp). The single source for the monitor's clock.
export function clockTime(at?: string | null): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
}
