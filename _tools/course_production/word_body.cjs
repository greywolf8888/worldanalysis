// Read legacy DOC body artifacts as data without opening Office or macros.
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const runtime = process.env.WORLDANALYSIS_COURSE_RUNTIME || path.join(root, ".runtime");
const WordExtractor = require(path.join(path.resolve(runtime), "course-method-doc/node_modules/word-extractor"));
async function main() {
  const parser = new WordExtractor();
  const doc = await parser.extract(fs.readFileSync(process.argv[2]));
  process.stdout.write(JSON.stringify({
    body: doc.getBody(),
    textboxes: doc.getTextboxes({includeHeadersAndFooters: false}),
    headerTextboxes: doc.getTextboxes({includeBody: false}),
    headers: doc.getHeaders({includeFooters: false}),
    footers: doc.getFooters(),
    footnotes: doc.getFootnotes(),
    endnotes: doc.getEndnotes(),
    annotations: doc.getAnnotations(),
  }));
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
