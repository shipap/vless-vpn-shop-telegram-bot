import { useEffect, useState } from "react";
import { messages } from "./messages";
import "./flag-font.css";
export function CustomEmoji({
  id,
  fallback = "✦",
  label,
}: {
  id?: string;
  fallback?: string;
  label?: string;
}) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [reduced, setReduced] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    update();
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [id]);
  const available =
    typeof id === "string" && /^[0-9]{1,32}$/.test(id) && !failed && !reduced;
  return (
    <span
      className="emoji custom-emoji"
      role="img"
      aria-label={label || fallback || messages.m4cf8915097}
    >
      {(!available || !loaded) && (
        <span aria-hidden="true" className="emoji-fallback">
          {fallback}
        </span>
      )}
      {available && (
        <img
          key={id}
          src={`/api/v1/content/emoji/${id}`}
          alt=""
          aria-hidden="true"
          width="24"
          height="24"
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          className={loaded ? "emoji-asset loaded" : "emoji-asset"}
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
        />
      )}
    </span>
  );
}
