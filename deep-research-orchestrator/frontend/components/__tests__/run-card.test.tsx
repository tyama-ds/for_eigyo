import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RunCard } from "../RunCard";
import type { RunLiveState } from "@/lib/sse-reducer";

function makeRun(overrides: Partial<RunLiveState> = {}): RunLiveState {
  return {
    runId: "run-a",
    engineId: "engine-a",
    status: "researching",
    stage: "search",
    attempt: 1,
    maxAttempts: 1,
    error: null,
    warnings: [],
    cancelRequested: false,
    createdAt: "2026-07-24T00:00:00Z",
    startedAt: null,
    finishedAt: null,
    elapsedSeconds: null,
    searchCount: 3,
    sourceCount: null,
    tokensInput: null,
    tokensOutput: null,
    tokensTotal: null,
    llmCostUsd: null,
    llmCostIsEstimate: false,
    searchApiCostUsd: null,
    infraCost: null,
    log: [],
    lastStatusSeq: 0,
    ...overrides,
  };
}

describe("RunCard", () => {
  it("renders 不明 for null tokens and never a fabricated 0", () => {
    render(<RunCard run={makeRun()} engineName="Engine A" />);
    const tokens = screen.getByTestId("metric-tokens");
    expect(tokens).toHaveTextContent("不明");
    expect(tokens).not.toHaveTextContent("0");

    const llmCost = screen.getByTestId("metric-llm-cost");
    expect(llmCost).toHaveTextContent("不明");
    expect(llmCost).not.toHaveTextContent("0");

    const sourceCount = screen.getByTestId("metric-sources");
    expect(sourceCount).toHaveTextContent("不明");
    expect(sourceCount).not.toHaveTextContent("0");

    // Known metrics still render normally.
    expect(screen.getByTestId("metric-searches")).toHaveTextContent("3");
  });

  it("includes the warning reason next to 不明 when warnings mention tokens", () => {
    render(
      <RunCard
        run={makeRun({ warnings: ["engine did not report token usage"] })}
        engineName="Engine A"
      />,
    );
    expect(screen.getByTestId("metric-tokens")).toHaveTextContent(
      "engine did not report token usage",
    );
  });

  it("renders measured token/cost values with the estimate tag", () => {
    render(
      <RunCard
        run={makeRun({
          tokensTotal: 1234,
          tokensInput: 1000,
          tokensOutput: 234,
          llmCostUsd: 0.5,
          llmCostIsEstimate: true,
        })}
        engineName="Engine A"
      />,
    );
    expect(screen.getByTestId("metric-tokens")).toHaveTextContent("1,234");
    const llmCost = screen.getByTestId("metric-llm-cost");
    expect(llmCost).toHaveTextContent("$0.50");
    expect(llmCost).toHaveTextContent("推定");
  });

  it("renders search api cost 0 as self-hosted ¥0 and infra not_measured as 計測対象外", () => {
    render(
      <RunCard
        run={makeRun({ searchApiCostUsd: 0, infraCost: "not_measured" })}
        engineName="Engine A"
      />,
    );
    expect(screen.getByTestId("metric-search-cost")).toHaveTextContent(
      "¥0（セルフホスト）",
    );
    expect(screen.getByTestId("metric-infra-cost")).toHaveTextContent(
      "計測対象外",
    );
  });

  it("shows status as icon + text label, not color alone", () => {
    const { container } = render(
      <RunCard run={makeRun({ status: "failed" })} engineName="Engine A" />,
    );
    expect(screen.getByText("失敗")).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
  });
});
