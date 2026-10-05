import { BackendStatus } from "@/components/backend-status";
import Link from "next/link";

export default function Home() {
  return (
    <div className="workspace">
      <header className="site-header">
        <Link className="wordmark" href="/" aria-label="SiteSense AI home">
          <span className="brand-mark" aria-hidden="true" />
          SiteSense <span className="brand-ai">AI</span>
        </Link>
        <span className="project-label">Research workspace</span>
      </header>

      <main>
        <section className="intro" aria-labelledby="project-heading">
          <p className="eyebrow">Location. Weather. Activity.</p>
          <h1 id="project-heading">
            Weather-aware retail location intelligence.
          </h1>
          <p className="intro-description">
            Explore how local weather relates to recorded check-in activity,
            then use that evidence to evaluate potential retail locations.
          </p>
        </section>

        <div className="status-grid">
          <section className="status-card" aria-labelledby="research-heading">
            <div className="card-heading">
              <h2 id="research-heading">Research readiness</h2>
              <span className="neutral-badge">Not configured</span>
            </div>
            <p className="card-title">Start with trustworthy data.</p>
            <p className="card-description">
              No check-in dataset, weather source, or trained model is connected
              yet. Activity forecasts and location scores will appear after data
              is prepared and models are evaluated.
            </p>
          </section>
          <BackendStatus />
        </div>

        <section className="research-path" aria-labelledby="path-heading">
          <h2 id="path-heading">The research path</h2>
          <ol className="path-list">
            <li>
              <span className="step-number">01</span>
              <h3>Prepare the evidence</h3>
              <p>
                Align recorded check-ins, locations, and historical weather.
              </p>
            </li>
            <li>
              <span className="step-number">02</span>
              <h3>Evaluate the models</h3>
              <p>Compare baselines and measure performance on held-out data.</p>
            </li>
            <li>
              <span className="step-number">03</span>
              <h3>Explore the locations</h3>
              <p>Present predictions with their assumptions and uncertainty.</p>
            </li>
          </ol>
        </section>
      </main>

      <footer className="site-footer">
        SiteSense AI · Retail location intelligence research
      </footer>
    </div>
  );
}
