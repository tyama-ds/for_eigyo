import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { JobForm } from "../JobForm";
import type { EngineView } from "@/lib/api-types";

function makeEngine(overrides: Partial<EngineView> = {}): EngineView {
  return {
    engine_id: "mock-fast",
    display_name: "Mock Fast",
    enabled: true,
    availability: "available",
    unavailable_reason: null,
    max_concurrency: 2,
    capabilities: {},
    healthy: true,
    ...overrides,
  };
}

describe("JobForm engine tiles", () => {
  it("exposes a real checkbox named after the engine display name", () => {
    render(
      <JobForm
        engines={[makeEngine()]}
        enginesError={null}
        onCreated={() => {}}
      />,
    );
    const checkbox = screen.getByRole("checkbox", { name: /Mock Fast/ });
    expect(checkbox).toBeInTheDocument();
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();
  });

  it("disables the checkbox for unavailable engines and shows the reason", () => {
    render(
      <JobForm
        engines={[
          makeEngine({
            engine_id: "mock-fail",
            display_name: "Mock Fail",
            availability: "disabled",
            enabled: false,
            unavailable_reason: "設定で無効化されています",
          }),
        ]}
        enginesError={null}
        onCreated={() => {}}
      />,
    );
    const checkbox = screen.getByRole("checkbox", { name: /Mock Fail/ });
    expect(checkbox).toBeDisabled();
    expect(screen.getByText("設定で無効化されています")).toBeInTheDocument();
  });
});
