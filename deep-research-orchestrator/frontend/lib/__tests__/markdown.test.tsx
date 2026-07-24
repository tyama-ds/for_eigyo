import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import {
  blocksToText,
  parseMarkdown,
  sanitizeUrl,
  stripHtmlTags,
} from "../markdown";
import { Markdown } from "@/components/Markdown";

describe("markdown sanitizer", () => {
  it("strips <script> tags and their bodies", () => {
    const blocks = parseMarkdown(
      "before <script>alert('xss')</script> after",
    );
    const text = blocksToText(blocks);
    expect(text).not.toContain("<script");
    expect(text).not.toContain("alert");
    expect(text).toContain("before");
    expect(text).toContain("after");
  });

  it("strips generic HTML tags but keeps their inner text", () => {
    const text = stripHtmlTags('<img src=x onerror="alert(1)"> <b>bold</b>');
    expect(text).not.toContain("<img");
    expect(text).not.toContain("onerror");
    expect(text).toContain("bold");
  });

  it("renders no script element and no raw HTML via the component", () => {
    const { container } = render(
      <Markdown source={"# title\n\nhello <script>alert(1)</script> world"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).not.toContain("<script");
    expect(container.textContent).toContain("hello");
    expect(container.textContent).toContain("world");
  });

  it("rejects javascript: URLs and renders the label as plain text", () => {
    expect(sanitizeUrl("javascript:alert(1)")).toBeNull();
    expect(sanitizeUrl("java\nscript:alert(1)")).toBeNull();
    expect(sanitizeUrl("https://example.com/a")).toBe("https://example.com/a");

    const { container } = render(
      <Markdown source={"[click me](javascript:alert(1))"} />,
    );
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("click me");
  });

  it("adds rel=noopener noreferrer and target=_blank to external links", () => {
    const { container } = render(
      <Markdown source={"[site](https://example.com/)"} />,
    );
    const a = container.querySelector("a");
    expect(a).not.toBeNull();
    expect(a?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(a?.getAttribute("target")).toBe("_blank");
  });

  it("parses headings, lists, bold and inline code", () => {
    const { container } = render(
      <Markdown
        source={"# H1\n\n- item **bold** `code`\n\n1. first\n2. second"}
      />,
    );
    expect(container.querySelector("h3")?.textContent).toBe("H1");
    expect(container.querySelector("ul li")?.textContent).toContain("item");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("renders [S1] citation chips when enabled", () => {
    const { getByRole } = render(
      <Markdown source={"finding [S1]"} citations onCitationClick={() => {}} />,
    );
    expect(getByRole("button")).toHaveTextContent("S1");
  });
});
