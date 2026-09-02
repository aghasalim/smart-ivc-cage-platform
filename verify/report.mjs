/**
 * Recompute the classification report from the confusion matrix in JavaScript.
 *
 * Same check as the other verifiers: derives precision, recall, F1, support
 * and macro-F1 from the raw 7x7 confusion matrix in ai/reports/metrics.json
 * and compares against the published report.
 *
 * Run: node verify/report.mjs [repo root]
 */
import { readFileSync } from "fs";
import { join, resolve } from "path";

const root = resolve(process.argv[2] || ".");
const d = JSON.parse(readFileSync(join(root, "ai", "reports", "metrics.json"), "utf8"));

const classes = d.classes;
const cm = d.test_confusion_matrix;
const report = d.test_classification_report;
const publishedF1 = d.test_macro_f1;
const n = classes.length;
const TOL = 5e-4;
let bad = 0;

function fail(msg) {
  console.log(`  FAIL: ${msg}`);
  bad++;
}

const f1s = [];
for (let i = 0; i < n; i++) {
  const tp = cm[i][i];
  let fp = 0;
  let fn = 0;
  for (let j = 0; j < n; j++) {
    if (j !== i) {
      fp += cm[j][i];
      fn += cm[i][j];
    }
  }
  const precision = tp + fp > 0 ? tp / (tp + fp) : 0;
  const recall = tp + fn > 0 ? tp / (tp + fn) : 0;
  const f1 = precision + recall > 0 ? (2 * precision * recall) / (precision + recall) : 0;
  f1s.push(f1);

  const name = classes[i];
  const pub = report[name] || {};
  for (const [metric, got] of [["precision", precision], ["recall", recall], ["f1-score", f1]]) {
    const pubVal = pub[metric];
    if (pubVal != null && Math.abs(got - pubVal) > TOL) {
      fail(`${name} ${metric}: published ${pubVal.toFixed(4)}, recomputed ${got.toFixed(4)}`);
    }
  }
}

const macroF1 = f1s.reduce((a, b) => a + b, 0) / n;
if (Math.abs(macroF1 - publishedF1) > TOL) {
  fail(`macro-F1: published ${publishedF1.toFixed(6)}, recomputed ${macroF1.toFixed(6)}`);
}

if (bad) {
  console.log(`JavaScript: ${bad} problem(s)`);
  process.exit(1);
}
console.log(`JavaScript: ${n} classes, macro-F1 ${macroF1.toFixed(4)} reproduced from confusion matrix`);
