import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConflictList } from "../tabs/ConflictsTab";

describe("ConflictList", () => {
  it("renders both conflicting values side by side with engine + claim", () => {
    render(
      <ConflictList
        conflicts={[
          {
            topic: "創業年",
            entries: [
              {
                engine_id: "engine-a",
                claim: "同社は2019年に設立された",
                value: "2019年",
              },
              {
                engine_id: "engine-b",
                claim: "同社は2021年に設立された",
                value: "2021年",
              },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText("創業年")).toBeInTheDocument();
    expect(screen.getByText("2019年")).toBeInTheDocument();
    expect(screen.getByText("2021年")).toBeInTheDocument();
    expect(screen.getByText("engine-a")).toBeInTheDocument();
    expect(screen.getByText("engine-b")).toBeInTheDocument();
    expect(screen.getByText("同社は2019年に設立された")).toBeInTheDocument();
    expect(screen.getByText("同社は2021年に設立された")).toBeInTheDocument();
  });

  it("supports the values-map shape as well", () => {
    render(
      <ConflictList
        conflicts={[
          {
            key: "市場規模",
            values: { "engine-a": "10億ドル", "engine-b": "25億ドル" },
          },
        ]}
      />,
    );
    expect(screen.getByText("10億ドル")).toBeInTheDocument();
    expect(screen.getByText("25億ドル")).toBeInTheDocument();
  });

  it("renders an empty message when there are no conflicts", () => {
    render(<ConflictList conflicts={[]} />);
    expect(screen.getByText("不一致は検出されませんでした")).toBeInTheDocument();
  });
});
