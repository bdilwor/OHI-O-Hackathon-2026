const COLORS = { High: '#2ecc8f', Medium: '#f5a524', Low: '#f05252' };

/** One confidence meter per answer; below 60% it lists the data that would make the answer more certain. */
export default function ConfidenceMeter({ confidence }) {
  if (!confidence) return null;
  const pctVal = Math.round(confidence.score * 100);
  const color = COLORS[confidence.label] || '#93a4bf';
  return (
    <div className="meter" role="group" aria-label={`Confidence ${pctVal}%, ${confidence.label}`}>
      <div className="meter-head">
        <span>Confidence</span>
        <span style={{ color }}><b>{pctVal}%</b> · {confidence.label}</span>
      </div>
      <div className="meter-track" aria-hidden="true">
        <div className="meter-fill" style={{ width: `${pctVal}%`, background: color }} />
        <div className="meter-mark" title="60%" />
      </div>
      {confidence.show_missing && confidence.missing?.length > 0 && (
        <div className="missing">
          <span>Missing data that would make this more certain:</span>
          <ul>{confidence.missing.map((m) => <li key={m}>{m}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
