import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { JSDOM } from "jsdom";

import { renderMarkdown } from "../static/markdown.js";


let dom;

before(() => {
  dom = new JSDOM("<!doctype html><main id='target'></main>", {
    url: "https://aur-bataao.test/tasks",
  });
  globalThis.document = dom.window.document;
  globalThis.window = dom.window;
});

after(() => {
  dom.window.close();
  delete globalThis.document;
  delete globalThis.window;
});

function render(source) {
  const target = document.querySelector("#target");
  renderMarkdown(target, source);
  return target;
}

test("renders block Markdown and normalizes source values", () => {
  let target = render(null);
  assert.equal(target.innerHTML, "");
  assert(target.classList.contains("markdown-body"));

  target = render([
    "# Heading #",
    "",
    "First line",
    "second line",
    "",
    "---",
    "",
    "> Quoted **strong** text",
    "> on two lines",
  ].join("\n"));

  assert.equal(target.querySelector("h1").textContent, "Heading");
  assert.equal(target.querySelector("p").innerHTML, "First line<br>second line");
  assert(target.querySelector("hr"));
  assert.equal(target.querySelector("blockquote strong").textContent, "strong");
  assert.equal(target.querySelector("blockquote p").textContent, "Quoted strong texton two lines");

  target = render("> Quote\nplain after quote");
  assert.equal(target.querySelector("blockquote").textContent, "Quote");
  assert.equal(target.querySelector("blockquote + p").textContent, "plain after quote");

  target = render("paragraph\n## Adjacent heading");
  assert.equal(target.querySelector("p").textContent, "paragraph");
  assert.equal(target.querySelector("h2").textContent, "Adjacent heading");
});

test("renders inline formatting, escapes, and code spans", () => {
  const target = render(
    String.raw`\*literal* **bold _inside_** __also bold__ *em* _too_ ~~gone~~ `
      + "` code ` ``two`` `unterminated",
  );

  assert.match(target.textContent, /\*literal\*/);
  assert.equal(target.querySelectorAll("strong").length, 2);
  assert.equal(target.querySelectorAll("em").length, 3);
  assert.equal(target.querySelector("del").textContent, "gone");
  assert.deepEqual(
    [...target.querySelectorAll("code")].map((element) => element.textContent),
    ["code", "two"],
  );
  assert.match(target.textContent, /`unterminated$/);

  const spaces = render("`   `");
  assert.equal(spaces.querySelector("code").textContent, "   ");
});

test("renders safe links and images without allowing unsafe protocols", () => {
  const target = render([
    "[external](https://example.test/path \"Example\")",
    "[relative](docs)",
    "[root](/tasks)",
    "[fragment](#today)",
    "[mail](mailto:person@example.test)",
    "[unsafe](javascript:alert(1))",
    "[broken](http://[)",
    "<https://example.test/auto>",
    "<mailto:person@example.test>",
    "![safe](/image.png 'Image')",
    "![without title](/untitled.png)",
    "![unsafe](data:text/plain,nope)",
  ].join("\n\n"));

  const links = [...target.querySelectorAll("a")];
  assert.equal(links.length, 7);
  assert.equal(links[0].target, "_blank");
  assert.equal(links[0].rel, "noopener noreferrer");
  assert.equal(links[0].title, "Example");
  assert.equal(links[1].href, "https://aur-bataao.test/docs");
  assert.equal(links[2].getAttribute("href"), "/tasks");
  assert.equal(links[3].getAttribute("href"), "#today");
  assert.equal(links[4].target, "");
  assert.equal(links[6].textContent, "person@example.test");
  assert.match(target.textContent, /\[unsafe\]/);
  assert.match(target.textContent, /\[broken\]/);

  const images = target.querySelectorAll("img");
  assert.equal(images.length, 2);
  assert.equal(images[0].alt, "safe");
  assert.equal(images[0].title, "Image");
  assert.equal(images[0].loading, "lazy");
  assert.equal(images[1].title, "");
  assert.match(target.textContent, /!\[unsafe\]/);
});

test("renders fenced code, including language sanitization and missing close fence", () => {
  let target = render("```py<script>\nprint('hello')\n```");
  assert.equal(target.querySelector("code").className, "language-pyscript");
  assert.equal(target.querySelector("code").textContent, "print('hello')");

  target = render("~~~~\nunclosed\nfence");
  assert.equal(target.querySelector("code").className, "");
  assert.equal(target.querySelector("code").textContent, "unclosed\nfence");
});

test("renders ordered, unordered, and task lists", () => {
  const target = render([
    "3. Third",
    "4) Fourth",
    "",
    "- [x] Done",
    "* [ ] Open",
    "+ Ordinary",
  ].join("\n"));

  const ordered = target.querySelector("ol");
  assert.equal(ordered.start, 3);
  assert.deepEqual([...ordered.children].map((item) => item.textContent), ["Third", "Fourth"]);
  const unordered = target.querySelector("ul");
  assert.equal(unordered.children.length, 3);
  const boxes = unordered.querySelectorAll("input[type=checkbox]");
  assert.equal(boxes.length, 2);
  assert.equal(boxes[0].checked, true);
  assert.equal(boxes[1].checked, false);
  assert.equal(boxes[0].disabled, true);
});

test("renders tables with alignment and escaped pipes", () => {
  const target = render([
    "| Left | Center | Right | Extra |",
    "| --- | :---: | ---: | --- |",
    String.raw`| a\|b | **c** | d | e |`,
    "",
    "after",
  ].join("\n"));

  const headings = [...target.querySelectorAll("th")];
  assert.deepEqual(headings.map((cell) => cell.style.textAlign), ["left", "center", "right", "left"]);
  assert.equal(target.querySelector("td").textContent, "a|b");
  assert.equal(target.querySelector("td strong").textContent, "c");
  assert.equal(target.querySelector(".markdown-table-wrap + p").textContent, "after");

  const uneven = render("A | B\n--- | ---\none | two | three");
  assert.equal(uneven.querySelectorAll("td")[2].style.textAlign, "left");

  const unevenHeader = render("A | B | C\n--- | ---\none | two");
  assert.equal(unevenHeader.querySelectorAll("th")[2].style.textAlign, "left");

  const adjacent = render("A | B\n--- | ---\none | two\nafter table");
  assert.equal(
    adjacent.querySelector(".markdown-table-wrap + p").textContent,
    "after table",
  );
});

test("does not treat invalid table dividers or mixed list types as one block", () => {
  const target = render("A | B\n-- | ---\n\n- one\n1. two");
  assert.equal(target.querySelector("table"), null);
  assert.equal(target.querySelectorAll("p").length, 1);
  assert.equal(target.querySelectorAll("ul").length, 1);
  assert.equal(target.querySelectorAll("ol").length, 1);
});
