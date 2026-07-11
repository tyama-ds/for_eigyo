/* formula.js — 式ツリーをHTML(分数バーつき)として描画する(MathJax同等手段)。
 * すべてtextContentで挿入するためXSS安全。パラメータはクリック可能。 */
(function () {
  "use strict";

  function span(cls, textStr) {
    const s = document.createElement("span");
    if (cls) s.className = cls;
    if (textStr !== undefined) s.textContent = textStr;
    return s;
  }

  const PRECEDENCE = { "+": 1, "-": 1, "*": 2, "/": 2, "**": 3 };

  /**
   * node: {kind, parameter_id, op, value, children}
   * labels: {parameter_id: 表示名}
   * onClick: (parameter_id) => void
   */
  function render(node, labels, onClick, parentPrec) {
    parentPrec = parentPrec || 0;
    if (!node || typeof node !== "object") {
      return span("fconst", "?");
    }
    if (node.kind === "constant") {
      return span("fconst", String(node.value));
    }
    if (node.kind === "parameter") {
      const v = span("fvar", labels[node.parameter_id] || node.parameter_id);
      v.tabIndex = 0;
      v.setAttribute("role", "button");
      v.title = node.parameter_id;
      if (onClick) {
        v.addEventListener("click", () => onClick(node.parameter_id));
        v.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(node.parameter_id); }
        });
      }
      return v;
    }
    const children = Array.isArray(node.children) ? node.children : [];
    if (!children.length) {
      return span("fconst", labels[node.parameter_id] || node.op || "?");
    }
    // 除算は分数として表示
    if (node.op === "/" && children.length === 2) {
      const frac = span("frac");
      const top = span("top");
      top.appendChild(render(node.children[0], labels, onClick, 0));
      const bottom = span("bottom");
      bottom.appendChild(render(node.children[1], labels, onClick, 0));
      frac.appendChild(top);
      frac.appendChild(bottom);
      return frac;
    }
    const prec = PRECEDENCE[node.op] || 0;
    const wrap = span("fexpr");
    const needParen = prec < parentPrec;
    if (needParen) wrap.appendChild(span("", "("));
    children.forEach((child, i) => {
      if (i > 0) wrap.appendChild(span("fop", ` ${node.op === "*" ? "×" : node.op} `));
      wrap.appendChild(render(child, labels, onClick, prec + (i > 0 && node.op === "-" ? 1 : 0)));
    });
    if (needParen) wrap.appendChild(span("", ")"));
    return wrap;
  }

  /** 分解ツリー(ul/li)を描画 */
  function renderTree(node, labels, onClick) {
    const ul = document.createElement("ul");
    ul.className = "tree";
    const li = document.createElement("li");
    if (node.kind === "parameter") {
      li.appendChild(render(node, labels, onClick, 0));
    } else if (node.kind === "constant") {
      li.textContent = String(node.value);
    } else {
      const opNames = { "*": "積", "/": "商", "+": "和", "-": "差", "**": "べき乗" };
      li.appendChild(span("fop", `${opNames[node.op] || node.op}`));
      node.children.forEach((c) => li.appendChild(renderTree(c, labels, onClick)));
    }
    ul.appendChild(li);
    return ul;
  }

  window.FermiFormula = { render, renderTree };
})();
