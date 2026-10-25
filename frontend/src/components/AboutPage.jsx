// "How it works", one page per portal, each section tagged with the HackOHI/O rubric row it answers.
// Every number here is copied from backend/ml/artifacts/METRICS.md, BENCHMARK.md, hazard_curve.json,
// fno_results.json or ASSUMPTIONS.md, or from our own in-browser timing tests (labeled as such).

const CUSTOMER = {
  tag: 'How My home works',
  title: 'Predict. Explain. Prepare. Communicate.',
  intro: 'My home tells an AEP customer, before and during a storm, how likely an outage is at their address, how long ' +
    'repairs could take and what to do, using AEP\'s own outage, grid and labor data. Every number comes from a tool ' +
    'that reads that data; the AI only puts it into words.',
  pipeline: [
    ['Find your home', 'Search an address or click the map; we match it to an AEP service location (or give a regional estimate outside AEP\'s data)'],
    ['Your supply path', 'The poles, wires, fuses and reclosers that feed your home, traced through AEP\'s upline topology'],
    ['Live forecast', 'Open-Meteo gusts for the next 7 days become a storm size through a curve fitted on 35 past storms'],
    ['Outage chance', 'Each piece of equipment on your path gets a damage chance; together: 1 − Π(1 − p)'],
    ['Repair range', 'Likely repairs × AEP STORMS labor hours × 4.5, simulated 2,000 times, upstream repairs first'],
    ['Your plan & chat', 'Prep steps and alert eligibility tailored to the needs you share, in a friendly voice'],
  ],
  metrics: [
    ['7.8x', 'better than chance at ranking damaged equipment'],
    ['0.81', 'ROC-AUC on storms the model never saw'],
    ['35', 'past storms learned from (2022–2026)'],
    ['2,000', 'repair simulations per estimate'],
    ['60%', 'confidence below which we list what data is missing'],
    ['0', 'registry or threat flags ever sent to the AI'],
  ],
  sections: [
    { rubric: 'Innovation', title: 'What we do differently', kind: 'diff', items: [
      ['Today\'s outage maps', 'show an outage after it happens, with one ETR and no reason.',
        'We predict before the storm, explain the top factors, and give a likely range with what could change it.'],
      ['Generic storm tips', 'are the same for everyone.',
        'Your plan adapts to the forecast, the expected outage length and the needs you choose to share.'],
      ['Black-box AI answers', 'can invent numbers.',
        'Every number comes from a tool; a reply with an unverified number is replaced by the rules-based answer.'],
    ] },
    { rubric: 'Curiosity', title: 'Questions we asked', kind: 'plain', items: [
      'The model learned from damage counts, but customers have a weather forecast. Can one become the other? (We fitted a gust → storm-size curve on real ERA5 weather for 35 storms.)',
      'How do we give a repair time without promising a false ETR? (A labor-hour simulation gives a range, not a point.)',
      'How do we help people with medical needs without exposing a registry? (We only use needs the customer declares.)',
      'What can we honestly say for a home outside AEP\'s data? (A regional typical value, clearly labeled, with low confidence.)',
    ] },
    { rubric: 'Breadth & Complexity', title: 'Fields combined in one answer', kind: 'plain', items: [
      'Meteorology: Open-Meteo forecasts and ERA5 reanalysis for every past storm.',
      'Power engineering: circuit GIS, upline topology, structure drawings and STORMS repair labor.',
      'Statistics: a calibrated model validated on whole held-out storms, and a Monte Carlo repair simulation.',
      'Human factors: plain language, a confidence meter, labeled estimates vs confirmed facts, accessibility needs.',
      'Privacy: IDs and exact coordinates redacted, every tool call audit-logged.',
      'At 10x the customers: risk is precomputed per asset and cached per storm size; the API is stateless.',
    ] },
    { rubric: 'Impact', title: 'Who it helps', kind: 'plain', items: [
      'Households get a personal outage chance, a repair range and a plan before the lights go out.',
      'People with medical or accessibility needs get tailored steps and callback eligibility, only from what they choose to share.',
      'AEP call centers get fewer "when is it back?" calls, because the answer, and why, is already in the app.',
    ] },
    { rubric: 'Growth Mindset', title: 'What didn\'t work, and what we changed', kind: 'pivots', items: [
      ['Our repair-duration model was no better than a constant.',
        'We report that honestly and estimate repairs from STORMS labor hours with a simulation instead.'],
      ['Hand-set gust anchors badly overestimated storms.',
        'A curve fitted on ERA5 weather expects 2,043 damage reports over the record where the old one expected 12,554 (actual: 1,659).'],
      ['The free LLM tier ran out of tokens mid-answer.',
        'The backend now runs the needed tools first and sends compact results: about half the tokens.'],
      ['A search like "Columbus, Ohio" just failed.',
        'It now offers a regional estimate built from the live forecast there and similar AEP-area homes.'],
      ['Switching tabs rebuilt the 3D map (about 15 s in our tests).',
        'Visited views now stay loaded and hidden maps pause: switching back takes about 0.1 s.'],
    ] },
    { rubric: 'Honest limits', title: 'What it can\'t do yet', kind: 'plain', items: [
      'Answers are model estimates, labeled as such and kept separate from confirmed AEP information.',
      'Wire repairs are a lower bound: STORMS has no conductor-restringing hours.',
      'There is no vegetation data; every confidence score accounts for it.',
      'The demo storm timeline is scripted; a production system would read AEP\'s outage-management feed.',
    ] },
  ],
};

const OPS = {
  tag: 'How the Operations side works',
  title: 'See the storm before it lands.',
  intro: 'The Operations page gives AEP staff the whole service area under one storm scenario (a forecast day or a ' +
    'simulated storm): expected damage, customers at risk, crews needed and critical facilities. The same scenario drives ' +
    'a detailed map of any home, an outage test and a staff-voice assistant, all from the model customers see.',
  pipeline: [
    ['Score every asset', '333k poles, wires and devices get a damage chance at the scenario\'s storm size'],
    ['Roll up circuits', 'Main-line chance per circuit = 1 − Π(1 − p) over its backbone'],
    ['KPIs', 'Expected damage, customers likely out, high-risk circuits, crew-hours'],
    ['Crews', 'Crew-hours ÷ 16-hour shifts for a 24 h and a 72 h restoration target'],
    ['Priority facilities', 'Hospitals, fire and ambulance stations linked to their circuit, with a suggested action'],
    ['Communicate', 'Plain-language SMS drafts and the alert and escalation rules customers receive'],
  ],
  metrics: [
    ['333k', 'assets scored per scenario'],
    ['121', 'circuits rolled up'],
    ['52', 'priority facilities tracked'],
    ['≥ 20%', 'main-line chance that marks a circuit high-risk'],
    ['16 h', 'storm shift used for crew counts'],
    ['≤ 0.25 s', 'area ↔ home map switch in our tests'],
  ],
  sections: [
    { rubric: 'Communication', title: 'What each number means', kind: 'glossary', items: [
      ['Expected damaged equipment', 'Sum of every asset\'s damage chance at the scenario\'s storm size.'],
      ['Customers likely out', 'Customers on each circuit × that circuit\'s main-line chance. Branch outages and step restoration are not modeled, so it is an approximation.'],
      ['High-risk circuits', 'Circuits whose main-line chance is in the High band (20% or more).'],
      ['Estimated crew-hours', 'Expected damage per asset × STORMS labor hours for its repair, including the 4.5× indirect-labor factor (travel, setup, safety).'],
      ['Crews to restore in 24 h / 72 h', 'Crew-hours ÷ (16-hour storm shifts × days). With a crew count entered: hours to restore all = crew-hours ÷ crews.'],
      ['Critical facilities at high risk', 'Public hospital, fire and ambulance locations on a high-risk circuit (nearest asset that has a circuit).'],
      ['Storm · Outage test · Chat', 'Storm sets the scenario and shows the area. Outage test places a fault at a home and previews what the customer sees. Chat briefs staff on that home. The map follows the tab.'],
    ] },
    { rubric: 'Innovation', title: 'What we do differently', kind: 'diff', items: [
      ['Outage management', 'is reactive: crews are sized after the calls come in.',
        'We size crews and flag facilities before the storm, from the forecast or a what-if storm.'],
      ['Separate internal tools', 'mean staff and customers see different stories.',
        'One model and one scenario drive the area numbers, the home map, the customer message and the staff briefing.'],
      ['Planning spreadsheets', 'hide their assumptions.',
        'Every tile states its formula, and every request is in the audit log.'],
    ] },
    { rubric: 'Curiosity', title: 'Questions we asked', kind: 'plain', items: [
      'Do tree models beat a simple one when only three storms are major? (Not on held-out storms; see below.)',
      'Would physics-informed features, monotone boosting, an explainable boosting machine or a neural operator do better?',
      'Could a physics-informed neural network (PINN) work? (No: pole damage has no governing equation to enforce.)',
      'Can crew needs come straight from AEP\'s own labor records? (Yes: STORMS repair hours × the indirect-labor factor.)',
    ] },
    { rubric: 'Breadth & Complexity', title: 'We benchmarked before choosing', kind: 'table',
      head: ['Model (same held-out storms)', 'Lift', 'ROC-AUC', 'Verdict'],
      rows: [
        ['Calibrated logistic regression (production)', '7.76x', '0.808', 'kept'],
        ['+ physics features (wind moment, span exposure)', '7.42x', '0.804', 'not adopted'],
        ['LightGBM with monotone constraints', '7.93x', '0.780', 'not adopted: ROC and Brier worse'],
        ['Rank ensemble', '7.47x', '0.794', 'not adopted'],
        ['Fourier Neural Operator (cell PR-AUC)', '0.09', '0.73', 'not adopted: production scores 0.17'],
      ],
      note: 'Adoption rule: ≥ 10% more lift, Brier not worse, ROC-AUC at most 0.01 lower, and the Feb 2025 storm not worse. ' +
        'Data from GIS, topology, ARCOS damage, STORMS labor, public facility layers and weather. At 10x: scoring is vectorized ' +
        'and cached per storm size, and another AEP region retrains the same pipeline without code changes.' },
    { rubric: 'Impact', title: 'Who it helps', kind: 'plain', items: [
      'Operations planners stage the right number of crews before the storm.',
      'Critical facilities get a call-ahead or a pre-staged crew when their circuit is high risk.',
      'Customer-facing teams send consistent, plain-language messages built from the same numbers customers see.',
      'Regulators and auditors can see every tool call and what was redacted.',
    ] },
    { rubric: 'Growth Mindset', title: 'What didn\'t work, and what we changed', kind: 'pivots', items: [
      ['Tree models looked promising but overfit individual storms.',
        'Leave-one-storm-out validation picked calibrated logistic regression (7.8x vs 5.1x for LightGBM).'],
      ['The EBM would not load in Docker on Apple Silicon.',
        'We documented it in BENCHMARK.md; it runs on an x86 machine.'],
      ['The neural operator found damaged areas about half as well.',
        'With 35 storms there is too little data to learn a weather-to-damage field; production stays explainable.'],
      ['A 50% main-line threshold never flagged a circuit.',
        'We use the app\'s High band (20%): no circuits at category 3, 22 at category 5.'],
      ['Three separate test tabs, and the map rebuilt on every switch (8–15 s in our tests).',
        'One Operations page; both maps stay loaded and hidden maps pause (≤ 0.25 s).'],
    ] },
    { rubric: 'Honest limits', title: 'What it can\'t do yet', kind: 'plain', items: [
      'No login: the internal portal is a prototype simplification.',
      'Wire repairs are a lower bound: STORMS has no conductor-restringing hours.',
      'Outage-test faults trip the nearest fuse or recloser on the same circuit within 400 m.',
      'Facilities are linked to the circuit of the nearest asset, not to AEP\'s customer records.',
    ] },
  ],
};

function Section({ s }) {
  return (
    <section aria-label={s.title}>
      <h2><span className="tag customer rubric-tag">{s.rubric}</span>{s.title}</h2>
      {s.kind === 'diff' && (
        <ul className="diff">
          {s.items.map(([a, b, c]) => <li key={a}><span className="was"><b>{a}</b> {b}</span><span className="now">{c}</span></li>)}
        </ul>
      )}
      {s.kind === 'pivots' && (
        <ul className="pivots">{s.items.map(([a, b]) => <li key={a}><span className="was">{a}</span><span className="now">{b}</span></li>)}</ul>
      )}
      {s.kind === 'plain' && <ul className="plain">{s.items.map((i) => <li key={i}>{i}</li>)}</ul>}
      {s.kind === 'glossary' && (
        <dl className="kpi-glossary">{s.items.map(([t, d]) => <div key={t}><dt>{t}</dt><dd>{d}</dd></div>)}</dl>
      )}
      {s.kind === 'table' && (
        <>
          <table className="ops-table">
            <thead><tr>{s.head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
            <tbody>{s.rows.map((r) => <tr key={r[0]}>{r.map((c, i) => <td key={s.head[i]}>{c}</td>)}</tr>)}</tbody>
          </table>
          <p className="hint-inline">{s.note}</p>
        </>
      )}
    </section>
  );
}

export default function AboutPage({ portal = 'customer' }) {
  const c = portal === 'ops' ? OPS : CUSTOMER;
  return (
    <main className="about">
      <header className="about-hero">
        <span className="tag customer">{c.tag}</span>
        <h1>{c.title}</h1>
        <p>{c.intro}</p>
      </header>

      <section aria-label="Pipeline">
        <h2><span className="tag customer rubric-tag">Communication</span>From data to a trusted answer</h2>
        <ol className="pipeline">{c.pipeline.map(([t, d]) => <li key={t}><b>{t}</b><span>{d}</span></li>)}</ol>
      </section>

      <section aria-label="By the numbers">
        <h2>By the numbers</h2>
        <div className="about-metrics">{c.metrics.map(([v, l]) => <div key={l}><b>{v}</b><span>{l}</span></div>)}</div>
      </section>

      <div className="about-grid">{c.sections.map((s) => <Section key={s.title + s.rubric} s={s} />)}</div>
    </main>
  );
}
