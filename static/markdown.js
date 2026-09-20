const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

function appendText(parent, text) {
  if (text) parent.append(document.createTextNode(text));
}

function safeLinkTarget(value) {
  const target = value.trim();
  if (target.startsWith("/") || target.startsWith("#")) return target;
  try {
    const url = new URL(target, window.location.href);
    return SAFE_LINK_PROTOCOLS.has(url.protocol) ? url.href : null;
  } catch (_) {
    return null;
  }
}

function matchingBackticks(text) {
  const opening = text.match(/^`+/)?.[0];
  if (!opening) return null;
  const closingIndex = text.indexOf(opening, opening.length);
  if (closingIndex < 0) return null;
  let content = text.slice(opening.length, closingIndex).replace(/\n/g, " ");
  if (/^ .* $/.test(content) && !/^ +$/.test(content)) content = content.slice(1, -1);
  return { content, length: closingIndex + opening.length };
}

function appendInline(parent, source) {
  let text = source;
  while (text) {
    const escaped = text.match(/^\\([\\`*{}\[\]()#+\-.!_>~|])/);
    if (escaped) {
      appendText(parent, escaped[1]);
      text = text.slice(escaped[0].length);
      continue;
    }

    const code = matchingBackticks(text);
    if (code) {
      const element = document.createElement("code");
      element.textContent = code.content;
      parent.append(element);
      text = text.slice(code.length);
      continue;
    }

    const image = text.match(/^!\[([^\]]*)\]\((\S+?)(?:\s+["']([^"']*)["'])?\)/);
    if (image) {
      const target = safeLinkTarget(image[2]);
      if (target) {
        const element = document.createElement("img");
        element.src = target;
        element.alt = image[1];
        element.loading = "lazy";
        if (image[3]) element.title = image[3];
        parent.append(element);
      } else {
        appendText(parent, image[0]);
      }
      text = text.slice(image[0].length);
      continue;
    }

    const link = text.match(/^\[([^\]]+)\]\((\S+?)(?:\s+["']([^"']*)["'])?\)/);
    if (link) {
      const target = safeLinkTarget(link[2]);
      if (target) {
        const element = document.createElement("a");
        element.href = target;
        if (link[3]) element.title = link[3];
        if (/^https?:/i.test(target)) {
          element.target = "_blank";
          element.rel = "noopener noreferrer";
        }
        appendInline(element, link[1]);
        parent.append(element);
      } else {
        appendText(parent, link[0]);
      }
      text = text.slice(link[0].length);
      continue;
    }

    const autolink = text.match(/^<((?:https?:\/\/|mailto:)[^ >]+)>/i);
    if (autolink) {
      const target = safeLinkTarget(autolink[1]);
      const element = document.createElement("a");
      element.href = target;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
      element.textContent = autolink[1].replace(/^mailto:/i, "");
      parent.append(element);
      text = text.slice(autolink[0].length);
      continue;
    }

    const strong = text.match(/^(\*\*|__)(?=\S)([\s\S]*?\S)\1/);
    if (strong) {
      const element = document.createElement("strong");
      appendInline(element, strong[2]);
      parent.append(element);
      text = text.slice(strong[0].length);
      continue;
    }

    const deleted = text.match(/^~~(?=\S)([\s\S]*?\S)~~/);
    if (deleted) {
      const element = document.createElement("del");
      appendInline(element, deleted[1]);
      parent.append(element);
      text = text.slice(deleted[0].length);
      continue;
    }

    const emphasis = text.match(/^(\*|_)(?=\S)([\s\S]*?\S)\1/);
    if (emphasis) {
      const element = document.createElement("em");
      appendInline(element, emphasis[2]);
      parent.append(element);
      text = text.slice(emphasis[0].length);
      continue;
    }

    if (text[0] === "\n") {
      parent.append(document.createElement("br"));
      text = text.slice(1);
      continue;
    }

    const nextSpecial = text.slice(1).search(/[\\`*!\[_~<\n]/);
    const length = nextSpecial < 0 ? text.length : nextSpecial + 1;
    appendText(parent, text.slice(0, length));
    text = text.slice(length);
  }
}

function splitTableRow(line) {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  return value.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function isTableDivider(line) {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function startsBlock(lines, index) {
  const line = lines[index] || "";
  return /^ {0,3}(#{1,6})\s+/.test(line)
    || /^ {0,3}(`{3,}|~{3,})/.test(line)
    || /^ {0,3}>/.test(line)
    || /^\s*(?:[-+*]|\d+[.)])\s+/.test(line)
    || /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)
    || (index + 1 < lines.length && line.includes("|") && isTableDivider(lines[index + 1]));
}

function appendTable(parent, lines, start) {
  const headers = splitTableRow(lines[start]);
  const alignments = splitTableRow(lines[start + 1]).map((cell) => {
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "left";
  });
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  headers.forEach((content, column) => {
    const cell = document.createElement("th");
    cell.style.textAlign = alignments[column] || "left";
    appendInline(cell, content);
    headerRow.append(cell);
  });
  head.append(headerRow);
  table.append(head);
  const body = document.createElement("tbody");
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
    const row = document.createElement("tr");
    splitTableRow(lines[index]).forEach((content, column) => {
      const cell = document.createElement("td");
      cell.style.textAlign = alignments[column] || "left";
      appendInline(cell, content);
      row.append(cell);
    });
    body.append(row);
    index += 1;
  }
  table.append(body);
  wrapper.append(table);
  parent.append(wrapper);
  return index;
}

function appendBlocks(parent, source) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^ {0,3}(`{3,}|~{3,})\s*([^ ]*)\s*$/);
    if (fence) {
      const content = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`).test(lines[index])) {
        content.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = content.join("\n");
      if (fence[2]) code.className = `language-${fence[2].replace(/[^a-z0-9_+-]/gi, "")}`;
      pre.append(code);
      parent.append(pre);
      continue;
    }

    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInline(element, heading[2]);
      parent.append(element);
      index += 1;
      continue;
    }

    if (/^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      parent.append(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (/^ {0,3}>/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^ {0,3}>/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^ {0,3}> ?/, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      appendBlocks(quote, quoteLines.join("\n"));
      parent.append(quote);
      continue;
    }

    if (index + 1 < lines.length && line.includes("|") && isTableDivider(lines[index + 1])) {
      index = appendTable(parent, lines, index);
      continue;
    }

    const listItem = line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
    if (listItem) {
      const ordered = /^\d/.test(listItem[1]);
      const list = document.createElement(ordered ? "ol" : "ul");
      if (ordered) list.start = Number.parseInt(listItem[1], 10);
      while (index < lines.length) {
        const match = lines[index].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
        if (!match || /^\d/.test(match[1]) !== ordered) break;
        const item = document.createElement("li");
        const task = match[2].match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          item.className = "task-list-item";
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.disabled = true;
          checkbox.checked = task[1].toLowerCase() === "x";
          item.append(checkbox);
          appendInline(item, task[2]);
        } else {
          appendInline(item, match[2]);
        }
        list.append(item);
        index += 1;
      }
      parent.append(list);
      continue;
    }

    const paragraphLines = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    const paragraph = document.createElement("p");
    appendInline(paragraph, paragraphLines.join("\n"));
    parent.append(paragraph);
  }
}

export function renderMarkdown(target, source) {
  target.replaceChildren();
  target.classList.add("markdown-body");
  appendBlocks(target, String(source ?? ""));
}
