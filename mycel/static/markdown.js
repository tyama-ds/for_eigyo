/* Mycel の Markdown レンダラ（依存なし）。
 * 対応: プロパティ(---) / 見出し / 段落 / 箇条書き（入れ子）/ 番号付き / チェックボックス /
 * 引用 / コードブロック / 表 / 水平線 / 強調・斜体・取り消し線 / インラインコード /
 * [[ウィキリンク]] / [テキスト](URL) / 画像 / #タグ / 自動リンク
 */
(function () {
  "use strict";
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^(https?:|mailto:|\/|\.|#)/i.test(u.trim()) ? u.trim() : "#");

  function inline(src, opt) {
    const slots = [];
    const hold = (html) => "\u0000" + (slots.push(html) - 1) + "\u0000";
    let s = src;
    s = s.replace(/`([^`\n]+)`/g, (m, c) => hold("<code>" + esc(c) + "</code>"));
    s = s.replace(/\[\[([^\[\]\n]+?)\]\]/g, (m, inner) => {
      let alias = "", heading = "", target = inner;
      if (target.includes("|")) [target, alias] = [target.slice(0, target.indexOf("|")), target.slice(target.indexOf("|") + 1)];
      if (target.includes("#")) [target, heading] = [target.slice(0, target.indexOf("#")), target.slice(target.indexOf("#") + 1)];
      target = target.trim();
      const path = opt.resolve ? opt.resolve(target) : null;
      const label = alias.trim() || (target + (heading ? " › " + heading.trim() : "")) || heading;
      return hold(`<a class="wl${path || !target ? "" : " unres"}" data-target="${esc(target)}" data-heading="${esc(heading.trim())}"${path ? ` data-path="${esc(path)}"` : ""} title="${esc(path ? path : target ? "未作成：クリックで作成" : "")}">${esc(label)}</a>`);
    });
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => hold(`<img alt="${esc(alt)}" src="${esc(safeUrl(url))}">`));
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, url) => hold(`<a class="ext" href="${esc(safeUrl(url))}" target="_blank" rel="noopener">${esc(t)}</a>`));
    s = s.replace(/\bhttps?:\/\/[^\s<>()　-ヿ一-鿿]+/g, (u) => hold(`<a class="ext" href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`));
    s = esc(s);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/__(.+?)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~(.+?)~~/g, "<del>$1</del>");
    s = s.replace(/==(.+?)==/g, "<mark>$1</mark>");
    s = s.replace(/(^|[\s(（、。])#([^\s#\[\](){}<>,.、。!?！？:;"'`|&\u0000]+)/g, (m, pre, tag) =>
      /^\d+$/.test(tag) ? m : `${pre}<span class="tag" data-tag="${tag}">#${tag}</span>`);
    s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => slots[+i]);
    return s;
  }

  function splitFrontmatter(text) {
    const lines = text.split("\n");
    if (lines[0] !== undefined && lines[0].trim() === "---") {
      for (let i = 1; i < Math.min(lines.length, 200); i++) {
        if (lines[i].trim() === "---") {
          const props = [];
          for (const l of lines.slice(1, i)) {
            const k = l.indexOf(":");
            if (k > 0) props.push([l.slice(0, k).trim(), l.slice(k + 1).trim()]);
          }
          return { props, start: i + 1 };
        }
      }
    }
    return { props: [], start: 0 };
  }

  function render(text, opt = {}) {
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    const fm = splitFrontmatter(text);
    let out = "";
    if (fm.props.length) {
      out += '<dl class="props">' + fm.props.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${inline(v, opt)}</dd>`).join("") + "</dl>";
    }
    let i = fm.start;
    const listStack = []; // {type, indent}
    let para = [];
    const slugs = {};
    const flushPara = () => { if (para.length) { out += "<p>" + para.map((l) => inline(l, opt)).join("<br>") + "</p>"; para = []; } };
    const closeLists = (toIndent = -1) => {
      while (listStack.length && listStack[listStack.length - 1].indent > toIndent) out += `</li></${listStack.pop().type}>`;
    };

    while (i < lines.length) {
      const line = lines[i];
      let m;
      if ((m = line.match(/^\s*(```|~~~)\s*([\w+-]*)/))) {
        flushPara(); closeLists();
        const fence = m[1]; const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        out += `<pre><code${m[2] ? ` data-lang="${esc(m[2])}"` : ""}>${esc(buf.join("\n"))}</code></pre>`;
        i++; continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/))) {
        flushPara(); closeLists();
        const lv = m[1].length; let slug = "h-" + m[2].trim();
        slugs[slug] = (slugs[slug] || 0) + 1; if (slugs[slug] > 1) slug += "-" + slugs[slug];
        out += `<h${lv} id="${esc(slug)}" data-line="${i}">${inline(m[2], opt)}</h${lv}>`;
        i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushPara(); closeLists(); out += "<hr>"; i++; continue; }
      if (/^\s*>/.test(line)) {
        flushPara(); closeLists();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out += "<blockquote>" + render(buf.join("\n"), { ...opt, nested: true }) + "</blockquote>";
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flushPara(); closeLists();
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line); i += 2;
        let t = "<table><thead><tr>" + head.map((c) => `<th>${inline(c, opt)}</th>`).join("") + "</tr></thead><tbody>";
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) t += "<tr>" + cells(lines[i++]).map((c) => `<td>${inline(c, opt)}</td>`).join("") + "</tr>";
        out += t + "</tbody></table>";
        continue;
      }
      if ((m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
        flushPara();
        const indent = m[1].replace(/\t/g, "  ").length;
        const type = /\d/.test(m[2]) ? "ol" : "ul";
        const top = listStack[listStack.length - 1];
        if (!top || indent > top.indent) {
          listStack.push({ type, indent });
          out += `<${type}>`;
        } else {
          closeLists(indent);
          const cur = listStack[listStack.length - 1];
          if (cur && cur.type !== type && cur.indent === indent) { out += `</li></${listStack.pop().type}><${type}>`; listStack.push({ type, indent }); }
          else if (cur) out += "</li>";
          else { listStack.push({ type, indent }); out += `<${type}>`; }
        }
        let body = m[3]; let task = body.match(/^\[([ xX])\]\s*(.*)$/);
        if (task) {
          const done = task[1] !== " ";
          out += `<li class="task${done ? " done" : ""}"><input type="checkbox" data-line="${i}"${done ? " checked" : ""}${opt.nested ? " disabled" : ""} aria-label="完了"><span class="tx">${inline(task[2], opt)}</span>`;
        } else out += `<li>${inline(body, opt)}`;
        i++; continue;
      }
      if (line.trim() === "") { flushPara(); if (listStack.length && !(lines[i + 1] || "").match(/^\s*([-*+]|\d+[.)])\s/)) closeLists(); i++; continue; }
      if (listStack.length && /^\s{2,}\S/.test(line)) { out += "<br>" + inline(line.trim(), opt); i++; continue; }
      closeLists();
      para.push(line);
      i++;
    }
    flushPara(); closeLists();
    return out;
  }

  window.MD = { render, esc, splitFrontmatter };
})();
