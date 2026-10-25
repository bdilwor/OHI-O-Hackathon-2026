// "How it works": the project's purpose, pipeline, research, pivots and limits, for customers, judges and AEP staff.
// Every number here is copied from backend/ml/artifacts/METRICS.md, BENCHMARK.md, hazard_curve.json or ASSUMPTIONS.md.

const PIPELINE = [
  ['AEP data', 'ARCOS damage reports, circuit GIS, upline topology, STORMS labor hours, structure drawings, priority facilities'],
  ['Outage-risk model', 'Calibrated logistic regression on 35 past storms: chance each pole, wire and device is damaged'],
  ['Live weather', 'Open-Meteo forecast → storm size, through a gust curve fitted on ERA5 weather for every past storm'],
  ['Restoration', 'Repair bundles × STORMS labor × 4.5 indirect factor, 2,000 simulations, upstream-first repair order'],
  ['Assistant', 'Tools answer; the LLM only phrases. Every number is checked against tool results'],
  ['Controls', 'Privacy filter, audit log, confidence meter, human escalation rules, regional fallback'],
];

const DIFFERENT = [
  ['Today\'s outage maps', 'show outages after they happen, with one ETR and no reason.',
    'We predict before the storm, explain the top factors, and give a likely range with what could change it.'],
  ['Generic storm tips', 'are the same for everyone.',
    'Guidance adapts to the forecast, the expected outage length and needs the customer chooses to share.'],
  ['Black-box AI answers', 'can invent numbers.',
    'Every number comes from a tool; replies with unverified numbers are replaced by the rules-based answer.'],
  ['Separate internal tools', 'mean customers and staff see different stories.',
    'Customers and AEP staff use the same model, in a friendly voice for customers and a briefing voice for staff.'],
];

const QUESTIONS = [
  'Which storm signals generalize to a storm the model has never seen? (We validate by holding out whole storms.)',
  'Can we predict from the weather forecast when the model learned from damage counts? (We fit a gust → storm-size curve on 35 storms of real ERA5 weather.)',
  'Would physics-informed or neural-operator models beat a simple one with this much data? (We tested them; see below.)',
  'How do we give a repair time without promising a false ETR? (Labor-hour simulation gives a range, not a point.)',
  'How do we help people with medical needs without exposing a registry? (Only needs the customer declares; the registry flag stays backend-only.)',
  'What can we say for a home outside AEP\'s data? (A regional typical value, clearly labeled, with low confidence.)',
];

const PIVOTS = [
  ['Tree models looked stronger, but overfit to individual storms.',
    'Leave-one-storm-out validation picked calibrated logistic regression: 7.8x better than chance at finding damaged equipment, ROC-AUC 0.81.'],
  ['Our repair-duration model was no better than a constant.',
    'We report that honestly and estimate restoration from STORMS labor hours with a Monte Carlo instead.'],
  ['The model learned from storm size, but customers have a weather forecast.',
    'We first used hand-set gust anchors, then fitted the curve to ERA5 weather: cross-validated storm-size error fell from 1.81 to 1.19 (log scale), and over the full record it expects 2,043 damage reports where the old curve expected 12,554 (actual: 1,659).'],
  ['Physics features, monotone gradient boosting and a Fourier Neural Operator (neuraloperator) sounded promising.',
    'All were benchmarked on the same held-out storms. None cleared our adoption bar: the FNO found damaged areas about half as well (cell PR-AUC 0.09 vs 0.17), with 35 storms too few to learn a weather-to-damage field. Production stays simple and explainable (BENCHMARK.md).'],
  ['The free LLM tier ran out of tokens mid-answer.',
    'The backend now runs the needed tools first and sends compact results: about half the tokens.'],
  ['The 3D storm test lagged.',
    'A spatial prefilter and caching cut the map query from 1.1 s to about 55 ms.'],
];

const METRICS = [
  ['35', 'past storms learned from (2022–2026)'],
  ['333k', 'poles, wires and devices scored'],
  ['7.8x', 'better than chance at ranking damaged equipment'],
  ['0.81', 'ROC-AUC on storms the model never saw'],
  ['34%', 'of damaged equipment found in the riskiest 5%'],
  ['2,000', 'restoration simulations per estimate'],
];

const SCALE = [
  'Scoring is vectorized over every asset and cached per storm size, so the whole service area updates in one to two seconds.',
  'At 10x the customers, the same design holds: precomputed per-asset risk, stateless endpoints, and per-storm caches.',
  'For another AEP region, the pipeline retrains on that region\'s GIS, topology and damage history without code changes.',
  'Outside AEP\'s data, location-specific accuracy needs that utility\'s grid data, or nationwide proxies (tree canopy, OpenStreetMap power lines) validated on outage records from several regions.',
];

export default function AboutPage() {
  return (
    <main className="about">
      <header className="about-hero">
        <span className="tag customer">How it works</span>
        <h1>Predict. Explain. Prepare. Communicate.</h1>
        <p>An AI assistant that tells AEP customers, before and during a storm, how likely an outage is, how long repairs could
          take and what to do, using AEP&apos;s own outage, grid and labor data. AEP staff use the same model to see the
          whole service area, test storms and outages, and send proactive messages.</p>
      </header>

      <section aria-labelledby="pipeline-h">
        <h2 id="pipeline-h">From AEP data to a trusted answer</h2>
        <ol className="pipeline">
          {PIPELINE.map(([t, d]) => <li key={t}><b>{t}</b><span>{d}</span></li>)}
        </ol>
      </section>

      <section aria-labelledby="metrics-h">
        <h2 id="metrics-h">By the numbers</h2>
        <div className="about-metrics">
          {METRICS.map(([v, l]) => <div key={l}><b>{v}</b><span>{l}</span></div>)}
        </div>
      </section>

      <div className="about-grid">
        <section aria-labelledby="diff-h">
          <h2 id="diff-h">What we do differently</h2>
          <ul className="diff">
            {DIFFERENT.map(([a, b, c]) => <li key={a}><span className="was"><b>{a}</b> {b}</span><span className="now">{c}</span></li>)}
          </ul>
        </section>
        <section aria-labelledby="q-h">
          <h2 id="q-h">Questions we asked</h2>
          <ul className="plain">{QUESTIONS.map((q) => <li key={q}>{q}</li>)}</ul>
        </section>
        <section aria-labelledby="pivot-h">
          <h2 id="pivot-h">What didn&apos;t work, and what we changed</h2>
          <ul className="pivots">
            {PIVOTS.map(([a, b]) => <li key={a}><span className="was">{a}</span><span className="now">{b}</span></li>)}
          </ul>
        </section>
        <section aria-labelledby="who-h">
          <h2 id="who-h">Who it helps</h2>
          <ul className="plain">
            <li><b>Customers</b> get a personal outage chance, a repair range and a plan, before the lights go out.</li>
            <li><b>People with medical or accessibility needs</b> get tailored steps and callback eligibility, only from what they choose to share.</li>
            <li><b>AEP operations</b> see which circuits, customers and critical facilities are at risk, and how many crews a storm needs.</li>
            <li><b>Customer-facing teams</b> get consistent, plain-language messages and briefings from the same numbers.</li>
          </ul>
        </section>
        <section aria-labelledby="scale-h">
          <h2 id="scale-h">At 10x scale, and beyond AEP&apos;s area</h2>
          <ul className="plain">{SCALE.map((s) => <li key={s}>{s}</li>)}</ul>
        </section>
        <section aria-labelledby="limits-h">
          <h2 id="limits-h">Honest limits</h2>
          <ul className="plain">
            <li>Answers are model estimates, labeled as such and kept separate from confirmed AEP information.</li>
            <li>Wire repairs are a lower bound: STORMS has no conductor-restringing hours.</li>
            <li>There is no vegetation data; every confidence score accounts for it.</li>
            <li>The demo storm timeline is scripted; a production system would read AEP&apos;s outage-management feed.</li>
          </ul>
        </section>
      </div>
    </main>
  );
}
