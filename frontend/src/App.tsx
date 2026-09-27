import { Navigate, NavLink, Route, Routes } from "react-router";
import styles from "./App.module.css";
import { HealthContainer } from "./features/health/HealthContainer";
import { OpportunityDetailContainer } from "./features/opportunities/OpportunityDetailContainer";
import { PortfolioContainer } from "./features/opportunities/PortfolioContainer";
import { CurrentRunContainer } from "./features/runs/CurrentRunContainer";
import { SettingsContainer } from "./features/settings/SettingsContainer";

export function App() {
  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main-content">
        Skip to main content
      </a>
      <header className={styles.header}>
        <NavLink className={styles.brand} to="/">
          Pensae Signal
        </NavLink>
        <nav aria-label="Primary navigation">
          <ul className={styles.navigation}>
            <li>
              <NavLink to="/">Current run</NavLink>
            </li>
            <li>
              <NavLink to="/opportunities">Opportunities</NavLink>
            </li>
            <li>
              <NavLink to="/settings">Settings</NavLink>
            </li>
          </ul>
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<HealthContainer />} />
        <Route path="/runs/:runId" element={<CurrentRunContainer />} />
        <Route path="/opportunities" element={<PortfolioContainer />} />
        <Route path="/opportunities/:opportunityId" element={<OpportunityDetailContainer />} />
        <Route path="/settings" element={<SettingsContainer />} />
        <Route path="*" element={<Navigate replace to="/" />} />
      </Routes>
    </div>
  );
}
