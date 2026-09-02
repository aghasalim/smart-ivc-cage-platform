# Recompute the classification report from the confusion matrix in Ruby.
#
# Run: ruby verify/report.rb <repo root>

require "json"

root = ARGV[0] || "."
d = JSON.parse(File.read(File.join(root, "ai", "reports", "metrics.json"), encoding: "UTF-8"))

classes = d["classes"]
cm = d["test_confusion_matrix"]
pub_f1 = d["test_macro_f1"]
n = classes.size
bad = 0
tol = 5e-4

fail_msg = ->(m) { puts "  FAIL: #{m}"; bad += 1 }

f1s = []
n.times do |i|
  tp = cm[i][i]
  fp = (0...n).select { |j| j != i }.sum { |j| cm[j][i] }
  fn = (0...n).select { |j| j != i }.sum { |j| cm[i][j] }
  prec = (tp + fp) > 0 ? tp.to_f / (tp + fp) : 0.0
  rec  = (tp + fn) > 0 ? tp.to_f / (tp + fn) : 0.0
  f1   = (prec + rec) > 0 ? 2.0 * prec * rec / (prec + rec) : 0.0
  f1s << f1
end

macro_f1 = f1s.sum / n
if (macro_f1 - pub_f1).abs > tol
  fail_msg.("macro-F1: published #{pub_f1}, recomputed #{macro_f1}")
end

if bad > 0
  puts "Ruby: #{bad} problem(s)"
  exit 1
end
puts "Ruby: #{n} classes, macro-F1 #{'%.4f' % macro_f1} reproduced from confusion matrix"
