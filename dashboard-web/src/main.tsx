import React from "react";
import ReactDOM from "react-dom/client";

import { DashboardApp } from "./dashboard/DashboardApp";
import { LocalFetchDashboardDataSource } from "./dashboard/local-fetch-data-source";
import "./styles.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Dashboard root element is missing.");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <DashboardApp dataSource={new LocalFetchDashboardDataSource()} />
  </React.StrictMode>,
);
