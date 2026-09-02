# Recompute the classification report from the confusion matrix in R.
#
# No external packages. Reads ai/reports/metrics.json line by line to extract
# the confusion matrix and macro-F1.
#
# Run: Rscript verify/report.R <repo root>

args <- commandArgs(trailingOnly = TRUE)
root <- if (length(args) > 0) args[1] else "."

lines <- readLines(file.path(root, "ai", "reports", "metrics.json"), warn = FALSE)
json_text <- paste(lines, collapse = " ")

# Extract test_macro_f1
pub_f1 <- as.numeric(regmatches(json_text,
  regexpr('(?<="test_macro_f1"\\s{0,5}:\\s{0,5})[0-9.eE+-]+', json_text, perl = TRUE)))

# Count classes by finding the classes array
cls_start <- regexpr('"classes"', json_text)
sub_text <- substring(json_text, cls_start)
bracket_start <- regexpr('\\[', sub_text)
bracket_end <- regexpr('\\]', sub_text)
cls_block <- substring(sub_text, bracket_start, bracket_end)
n <- length(regmatches(cls_block, gregexpr('"[^"]+"', cls_block))[[1]])

# Extract the confusion matrix
cm_start <- regexpr('"test_confusion_matrix"', json_text)
cm_sub <- substring(json_text, cm_start)
# Find all integers in the matrix block (up to the closing ]])
cm_close <- regexpr('\\]\\s*\\]', cm_sub)
cm_block <- substring(cm_sub, 1, cm_close + attr(cm_close, "match.length") - 1)
nums <- as.integer(regmatches(cm_block, gregexpr('[0-9]+', cm_block))[[1]])

if (length(nums) != n * n) {
  cat("FAIL: expected", n*n, "matrix entries, got", length(nums), "\n")
  quit(status = 1)
}
cm <- matrix(nums, nrow = n, byrow = TRUE)

bad <- 0
fail <- function(msg) { cat("  FAIL:", msg, "\n"); bad <<- bad + 1 }
TOL <- 5e-4

f1s <- numeric(n)
for (i in seq_len(n)) {
  tp <- cm[i, i]
  fp <- sum(cm[-i, i])
  fn <- sum(cm[i, -i])
  prec <- if ((tp + fp) > 0) tp / (tp + fp) else 0
  rec  <- if ((tp + fn) > 0) tp / (tp + fn) else 0
  f1   <- if ((prec + rec) > 0) 2 * prec * rec / (prec + rec) else 0
  f1s[i] <- f1
}

macro_f1 <- mean(f1s)
if (abs(macro_f1 - pub_f1) > TOL) {
  fail(sprintf("macro-F1: published %.6f, recomputed %.6f", pub_f1, macro_f1))
}

if (bad > 0) {
  cat("R:", bad, "problem(s)\n")
  quit(status = 1)
}
cat(sprintf("R: %d classes, macro-F1 %.4f reproduced from confusion matrix\n", n, macro_f1))
